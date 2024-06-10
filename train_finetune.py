# load packages
import copy
import os
import random
import time

import click
import numpy as np
import torch
import torch.nn.functional as F
import yaml
from monotonic_align import mask_from_lens
from munch import Munch
from torch.utils.tensorboard import SummaryWriter

from losses import DiscriminatorLoss, GeneratorLoss, MultiResolutionSTFTLoss, WavLMLoss
from meldataset import get_dataloaders
from models import build_model, load_checkpoint, load_pretrained_models
from Modules.diffusion.sampler import ADPM2Sampler, DiffusionSampler, KarrasSchedule
from Modules.slmadv import SLMAdversarialLoss
from optimizers import build_optimizer
from utils import (
    configure_environment,
    length_to_mask,
    log_norm,
    maximum_path,
    recursive_munch,
)


# simple fix for dataparallel that allows access to class attributes
class MyDataParallel(torch.nn.DataParallel):
    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.module, name)


@click.command()
@click.option("-p", "--config_path", default="Configs/config_ft.yml", type=str)
def main(config_path):
    # Load config and set up environment
    config, logger, log_dir = configure_environment(config_path)

    writer = SummaryWriter(log_dir + "/tensorboard")

    batch_size = config.get("batch_size", 10)
    epochs = config.get("epochs", 200)
    save_freq = config.get("save_freq", 2)
    log_interval = config.get("log_interval", 10)
    data_params = config.get("data_params", None)
    sr = config["preprocess_params"].get("sr", 24000)
    max_len = config.get("max_len", 200)
    loss_params = Munch(config["loss_params"])
    diff_epoch = loss_params.diff_epoch
    joint_epoch = loss_params.joint_epoch
    optimizer_params = Munch(config["optimizer_params"])
    device = "cuda"

    # Load the datasets
    train_dataloader, val_dataloader, train_list = get_dataloaders(
        dataset_config=data_params, batch_size=batch_size, num_workers=2, device=device
    )

    # load pretrained models
    text_aligner, pitch_extractor, plbert = load_pretrained_models(config)

    # build model
    model_params = recursive_munch(config["model_params"])
    multispeaker = model_params.multispeaker
    model = build_model(model_params, text_aligner, pitch_extractor, plbert)
    _ = [model[key].to(device) for key in model]

    # DP
    for key in model:
        if key != "mpd" and key != "msd" and key != "wd":
            model[key] = MyDataParallel(model[key])

    start_epoch = 0
    iters = 0

    load_pretrained = config.get("pretrained_model", "") != "" and config.get(
        "second_stage_load_pretrained", False
    )

    if not load_pretrained:
        if config.get("first_stage_path", "") != "":
            first_stage_path = os.path.join(
                log_dir, config.get("first_stage_path", "first_stage.pth")
            )
            print("Loading the first stage model at %s ..." % first_stage_path)
            model, _, start_epoch, iters = load_checkpoint(
                model,
                None,
                first_stage_path,
                load_only_params=True,
                ignore_modules=[
                    "bert",
                    "bert_encoder",
                    "predictor",
                    "predictor_encoder",
                    "msd",
                    "mpd",
                    "wd",
                    "diffusion",
                ],
            )  # keep starting epoch for tensorboard log

            # these epochs should be counted from the start epoch
            diff_epoch += start_epoch
            joint_epoch += start_epoch
            epochs += start_epoch

            model.predictor_encoder = copy.deepcopy(model.style_encoder)
        else:
            raise ValueError("You need to specify the path to the first stage model.")

    gl = GeneratorLoss(model.mpd, model.msd).to(device)
    dl = DiscriminatorLoss(model.mpd, model.msd).to(device)
    wl = WavLMLoss(model_params.slm.model, model.wd, sr, model_params.slm.sr).to(device)

    gl = MyDataParallel(gl)
    dl = MyDataParallel(dl)
    wl = MyDataParallel(wl)

    sampler = DiffusionSampler(
        model.diffusion.diffusion,
        sampler=ADPM2Sampler(),
        sigma_schedule=KarrasSchedule(
            sigma_min=0.0001, sigma_max=3.0, rho=9.0
        ),  # empirical parameters
        clamp=False,
    )

    scheduler_params = {
        "max_lr": optimizer_params.lr,
        "pct_start": float(0),
        "epochs": epochs,
        "steps_per_epoch": len(train_dataloader),
    }
    scheduler_params_dict = {key: scheduler_params.copy() for key in model}
    scheduler_params_dict["bert"]["max_lr"] = optimizer_params.bert_lr * 2
    scheduler_params_dict["decoder"]["max_lr"] = optimizer_params.ft_lr * 2
    scheduler_params_dict["style_encoder"]["max_lr"] = optimizer_params.ft_lr * 2

    optimizer = build_optimizer(
        {key: model[key].parameters() for key in model},
        scheduler_params_dict=scheduler_params_dict,
        lr=optimizer_params.lr,
    )

    # adjust BERT learning rate
    for g in optimizer.optimizers["bert"].param_groups:
        g["betas"] = (0.9, 0.99)
        g["lr"] = optimizer_params.bert_lr
        g["initial_lr"] = optimizer_params.bert_lr
        g["min_lr"] = 0
        g["weight_decay"] = 0.01

    # adjust acoustic module learning rate
    for module in ["decoder", "style_encoder"]:
        for g in optimizer.optimizers[module].param_groups:
            g["betas"] = (0.0, 0.99)
            g["lr"] = optimizer_params.ft_lr
            g["initial_lr"] = optimizer_params.ft_lr
            g["min_lr"] = 0
            g["weight_decay"] = 1e-4

    # load models if there is a model
    if load_pretrained:
        model, optimizer, start_epoch, iters = load_checkpoint(
            model,
            optimizer,
            config["pretrained_model"],
            load_only_params=config.get("load_only_params", True),
        )

    n_down = model.text_aligner.n_down

    best_loss = float("inf")  # best test loss
    iters = 0

    torch.cuda.empty_cache()

    stft_loss = MultiResolutionSTFTLoss().to(device)

    print("BERT", optimizer.optimizers["bert"])
    print("decoder", optimizer.optimizers["decoder"])

    running_std = []

    slmadv_params = Munch(config["slmadv_params"])
    slmadv = SLMAdversarialLoss(
        model,
        wl,
        sampler,
        slmadv_params.min_len,
        slmadv_params.max_len,
        batch_percentage=slmadv_params.batch_percentage,
        skip_update=slmadv_params.iter,
        sig=slmadv_params.sig,
    )

    for epoch in range(start_epoch, epochs):
        running_loss = 0
        start_time = time.time()

        _ = [model[key].eval() for key in model]

        model.text_aligner.train()
        model.text_encoder.train()

        model.predictor.train()
        model.bert_encoder.train()
        model.bert.train()
        model.msd.train()
        model.mpd.train()

        for i, batch in enumerate(train_dataloader):
            waves = batch[0]
            batch = [b.to(device) for b in batch[1:]]
            (
                texts,
                input_lengths,
                ref_texts,
                ref_lengths,
                mels,
                mel_input_length,
                ref_mels,
            ) = batch
            with torch.no_grad():
                mask = length_to_mask(mel_input_length // (2**n_down)).to(device)
                text_mask = length_to_mask(input_lengths).to(texts.device)

                # compute reference styles
                if multispeaker and epoch >= diff_epoch:
                    ref_ss = model.style_encoder(ref_mels.unsqueeze(1))
                    ref_sp = model.predictor_encoder(ref_mels.unsqueeze(1))
                    ref = torch.cat([ref_ss, ref_sp], dim=1)

            try:
                ppgs, s2s_pred, s2s_attn = model.text_aligner(mels, mask, texts)
                s2s_attn = s2s_attn.transpose(-1, -2)
                s2s_attn = s2s_attn[..., 1:]
                s2s_attn = s2s_attn.transpose(-1, -2)
            except Exception:
                continue

            mask_ST = mask_from_lens(
                s2s_attn, input_lengths, mel_input_length // (2**n_down)
            )
            s2s_attn_mono = maximum_path(s2s_attn, mask_ST)

            # encode
            t_en = model.text_encoder(texts, input_lengths, text_mask)

            # 50% of chance of using monotonic version
            if bool(random.getrandbits(1)):
                asr = t_en @ s2s_attn
            else:
                asr = t_en @ s2s_attn_mono

            d_gt = s2s_attn_mono.sum(axis=-1).detach()

            # compute the style of the entire utterance
            # this operation cannot be done in batch because of the avgpool layer (may need to work on masked avgpool)
            ss = []
            gs = []
            for bib in range(len(mel_input_length)):
                mel_length = int(mel_input_length[bib].item())
                mel = mels[bib, :, : mel_input_length[bib]]
                s = model.predictor_encoder(mel.unsqueeze(0).unsqueeze(1))
                ss.append(s)
                s = model.style_encoder(mel.unsqueeze(0).unsqueeze(1))
                gs.append(s)

            s_dur = torch.stack(ss).squeeze()  # global prosodic styles
            gs = torch.stack(gs).squeeze()  # global acoustic styles
            s_trg = torch.cat([gs, s_dur], dim=-1).detach()  # ground truth for denoiser

            bert_dur = model.bert(texts, attention_mask=(~text_mask).int())
            d_en = model.bert_encoder(bert_dur).transpose(-1, -2)

            # denoiser training
            if epoch >= diff_epoch:
                num_steps = np.random.randint(3, 5)

                if model_params.diffusion.dist.estimate_sigma_data:
                    model.diffusion.module.diffusion.sigma_data = (
                        s_trg.std(axis=-1).mean().item()
                    )  # batch-wise std estimation
                    running_std.append(model.diffusion.module.diffusion.sigma_data)

                if multispeaker:
                    s_preds = sampler(
                        noise=torch.randn_like(s_trg).unsqueeze(1).to(device),
                        embedding=bert_dur,
                        embedding_scale=1,
                        features=ref,  # reference from the same speaker as the embedding
                        embedding_mask_proba=0.1,
                        num_steps=num_steps,
                    ).squeeze(1)
                    loss_diff = model.diffusion(
                        s_trg.unsqueeze(1), embedding=bert_dur, features=ref
                    ).mean()  # EDM loss
                    loss_sty = F.l1_loss(
                        s_preds, s_trg.detach()
                    )  # style reconstruction loss
                else:
                    s_preds = sampler(
                        noise=torch.randn_like(s_trg).unsqueeze(1).to(device),
                        embedding=bert_dur,
                        embedding_scale=1,
                        embedding_mask_proba=0.1,
                        num_steps=num_steps,
                    ).squeeze(1)
                    loss_diff = model.diffusion.module.diffusion(
                        s_trg.unsqueeze(1), embedding=bert_dur
                    ).mean()  # EDM loss
                    loss_sty = F.l1_loss(
                        s_preds, s_trg.detach()
                    )  # style reconstruction loss
            else:
                loss_sty = 0
                loss_diff = 0

            s_loss = 0

            d, p = model.predictor(d_en, s_dur, input_lengths, s2s_attn_mono, text_mask)

            mel_len_st = int(mel_input_length.min().item() / 2 - 1)
            mel_len = min(int(mel_input_length.min().item() / 2 - 1), max_len // 2)
            en = []
            gt = []
            p_en = []
            wav = []
            st = []

            for bib in range(len(mel_input_length)):
                mel_length = int(mel_input_length[bib].item() / 2)

                random_start = np.random.randint(0, mel_length - mel_len)
                en.append(asr[bib, :, random_start : random_start + mel_len])
                p_en.append(p[bib, :, random_start : random_start + mel_len])
                gt.append(
                    mels[bib, :, (random_start * 2) : ((random_start + mel_len) * 2)]
                )

                y = waves[bib][
                    (random_start * 2) * 300 : ((random_start + mel_len) * 2) * 300
                ]
                wav.append(torch.from_numpy(y).to(device))

                # style reference (better to be different from the GT)
                random_start = np.random.randint(0, mel_length - mel_len_st)
                st.append(
                    mels[bib, :, (random_start * 2) : ((random_start + mel_len_st) * 2)]
                )

            wav = torch.stack(wav).float().detach()

            en = torch.stack(en)
            p_en = torch.stack(p_en)
            gt = torch.stack(gt).detach()
            st = torch.stack(st).detach()

            if gt.size(-1) < 80:
                continue

            s = model.style_encoder(gt.unsqueeze(1))
            s_dur = model.predictor_encoder(gt.unsqueeze(1))

            with torch.no_grad():
                F0_real, _, F0 = model.pitch_extractor(gt.unsqueeze(1))
                F0 = F0.reshape(F0.shape[0], F0.shape[1] * 2, F0.shape[2], 1).squeeze()

                N_real = log_norm(gt.unsqueeze(1)).squeeze(1)

                y_rec_gt = wav.unsqueeze(1)
                y_rec_gt_pred = model.decoder(en, F0_real, N_real, s)

                wav = y_rec_gt

            F0_fake, N_fake = model.predictor.F0Ntrain(p_en, s_dur)

            y_rec = model.decoder(en, F0_fake, N_fake, s)

            loss_F0_rec = (F.smooth_l1_loss(F0_real, F0_fake)) / 10
            loss_norm_rec = F.smooth_l1_loss(N_real, N_fake)

            optimizer.zero_grad()
            d_loss = dl(wav.detach(), y_rec.detach()).mean()
            d_loss.backward()
            optimizer.step("msd")
            optimizer.step("mpd")

            # generator loss
            optimizer.zero_grad()

            loss_mel = stft_loss(y_rec, wav)
            loss_gen_all = gl(wav, y_rec).mean()
            loss_lm = wl(wav.detach().squeeze(), y_rec.squeeze()).mean()

            loss_ce = 0
            loss_dur = 0
            for _s2s_pred, _text_input, _text_length in zip(d, (d_gt), input_lengths):
                _s2s_pred = _s2s_pred[:_text_length, :]
                _text_input = _text_input[:_text_length].long()
                _s2s_trg = torch.zeros_like(_s2s_pred)
                for p in range(_s2s_trg.shape[0]):
                    _s2s_trg[p, : _text_input[p]] = 1
                _dur_pred = torch.sigmoid(_s2s_pred).sum(axis=1)

                loss_dur += F.l1_loss(
                    _dur_pred[1 : _text_length - 1], _text_input[1 : _text_length - 1]
                )
                loss_ce += F.binary_cross_entropy_with_logits(
                    _s2s_pred.flatten(), _s2s_trg.flatten()
                )

            loss_ce /= texts.size(0)
            loss_dur /= texts.size(0)

            loss_s2s = 0
            for _s2s_pred, _text_input, _text_length in zip(
                s2s_pred, texts, input_lengths
            ):
                loss_s2s += F.cross_entropy(
                    _s2s_pred[:_text_length], _text_input[:_text_length]
                )
            loss_s2s /= texts.size(0)

            loss_mono = F.l1_loss(s2s_attn, s2s_attn_mono) * 10

            g_loss = (
                loss_params.lambda_mel * loss_mel
                + loss_params.lambda_F0 * loss_F0_rec
                + loss_params.lambda_ce * loss_ce
                + loss_params.lambda_norm * loss_norm_rec
                + loss_params.lambda_dur * loss_dur
                + loss_params.lambda_gen * loss_gen_all
                + loss_params.lambda_slm * loss_lm
                + loss_params.lambda_sty * loss_sty
                + loss_params.lambda_diff * loss_diff
                + loss_params.lambda_mono * loss_mono
                + loss_params.lambda_s2s * loss_s2s
            )

            running_loss += loss_mel.item()
            g_loss.backward()

            optimizer.step("bert_encoder")
            optimizer.step("bert")
            optimizer.step("predictor")
            optimizer.step("predictor_encoder")
            optimizer.step("style_encoder")
            optimizer.step("decoder")

            optimizer.step("text_encoder")
            optimizer.step("text_aligner")

            if epoch >= diff_epoch:
                optimizer.step("diffusion")

            d_loss_slm, loss_gen_lm = 0, 0
            if epoch >= joint_epoch:
                # randomly pick whether to use in-distribution text
                if np.random.rand() < 0.5:
                    use_ind = True
                else:
                    use_ind = False

                if use_ind:
                    ref_lengths = input_lengths
                    ref_texts = texts

                slm_out = slmadv(
                    i,
                    y_rec_gt,
                    y_rec_gt_pred,
                    waves,
                    mel_input_length,
                    ref_texts,
                    ref_lengths,
                    use_ind,
                    s_trg.detach(),
                    ref if multispeaker else None,
                )

                if slm_out is not None:
                    d_loss_slm, loss_gen_lm, y_pred = slm_out

                    # SLM generator loss
                    optimizer.zero_grad()
                    loss_gen_lm.backward()

                    # compute the gradient norm
                    total_norm = {}
                    for key in model.keys():
                        total_norm[key] = 0
                        parameters = [
                            p
                            for p in model[key].parameters()
                            if p.grad is not None and p.requires_grad
                        ]
                        for p in parameters:
                            param_norm = p.grad.detach().data.norm(2)
                            total_norm[key] += param_norm.item() ** 2
                        total_norm[key] = total_norm[key] ** 0.5

                    # gradient scaling
                    if total_norm["predictor"] > slmadv_params.thresh:
                        for key in model.keys():
                            for p in model[key].parameters():
                                if p.grad is not None:
                                    p.grad *= 1 / total_norm["predictor"]

                    for p in model.predictor.duration_proj.parameters():
                        if p.grad is not None:
                            p.grad *= slmadv_params.scale

                    for p in model.predictor.lstm.parameters():
                        if p.grad is not None:
                            p.grad *= slmadv_params.scale

                    for p in model.diffusion.parameters():
                        if p.grad is not None:
                            p.grad *= slmadv_params.scale

                    optimizer.step("bert_encoder")
                    optimizer.step("bert")
                    optimizer.step("predictor")
                    optimizer.step("diffusion")

                    # SLM discriminator loss
                    if d_loss_slm != 0:
                        optimizer.zero_grad()
                        d_loss_slm.backward(retain_graph=True)
                        optimizer.step("wd")

            iters = iters + 1

            if (i + 1) % log_interval == 0:
                logger.info(
                    "Epoch [%d/%d], Step [%d/%d], Loss: %.5f, Disc Loss: %.5f, Dur Loss: %.5f, CE Loss: %.5f, Norm Loss: %.5f, F0 Loss: %.5f, LM Loss: %.5f, Gen Loss: %.5f, Sty Loss: %.5f, Diff Loss: %.5f, DiscLM Loss: %.5f, GenLM Loss: %.5f, SLoss: %.5f, S2S Loss: %.5f, Mono Loss: %.5f"
                    % (
                        epoch + 1,
                        epochs,
                        i + 1,
                        len(train_list) // batch_size,
                        running_loss / log_interval,
                        d_loss,
                        loss_dur,
                        loss_ce,
                        loss_norm_rec,
                        loss_F0_rec,
                        loss_lm,
                        loss_gen_all,
                        loss_sty,
                        loss_diff,
                        d_loss_slm,
                        loss_gen_lm,
                        s_loss,
                        loss_s2s,
                        loss_mono,
                    )
                )

                writer.add_scalar("train/mel_loss", running_loss / log_interval, iters)
                writer.add_scalar("train/gen_loss", loss_gen_all, iters)
                writer.add_scalar("train/d_loss", d_loss, iters)
                writer.add_scalar("train/ce_loss", loss_ce, iters)
                writer.add_scalar("train/dur_loss", loss_dur, iters)
                writer.add_scalar("train/slm_loss", loss_lm, iters)
                writer.add_scalar("train/norm_loss", loss_norm_rec, iters)
                writer.add_scalar("train/F0_loss", loss_F0_rec, iters)
                writer.add_scalar("train/sty_loss", loss_sty, iters)
                writer.add_scalar("train/diff_loss", loss_diff, iters)
                writer.add_scalar("train/d_loss_slm", d_loss_slm, iters)
                writer.add_scalar("train/gen_loss_slm", loss_gen_lm, iters)

                running_loss = 0

                print("Time elasped:", time.time() - start_time)

        loss_test = 0
        loss_align = 0
        loss_f = 0
        _ = [model[key].eval() for key in model]

        with torch.no_grad():
            iters_test = 0
            for batch_idx, batch in enumerate(val_dataloader):
                optimizer.zero_grad()

                try:
                    waves = batch[0]
                    batch = [b.to(device) for b in batch[1:]]
                    (
                        texts,
                        input_lengths,
                        ref_texts,
                        ref_lengths,
                        mels,
                        mel_input_length,
                        ref_mels,
                    ) = batch
                    with torch.no_grad():
                        mask = length_to_mask(mel_input_length // (2**n_down)).to(
                            "cuda"
                        )
                        text_mask = length_to_mask(input_lengths).to(texts.device)

                        _, _, s2s_attn = model.text_aligner(mels, mask, texts)
                        s2s_attn = s2s_attn.transpose(-1, -2)
                        s2s_attn = s2s_attn[..., 1:]
                        s2s_attn = s2s_attn.transpose(-1, -2)

                        mask_ST = mask_from_lens(
                            s2s_attn, input_lengths, mel_input_length // (2**n_down)
                        )
                        s2s_attn_mono = maximum_path(s2s_attn, mask_ST)

                        # encode
                        t_en = model.text_encoder(texts, input_lengths, text_mask)
                        asr = t_en @ s2s_attn_mono

                        d_gt = s2s_attn_mono.sum(axis=-1).detach()

                    ss = []
                    gs = []

                    for bib in range(len(mel_input_length)):
                        mel_length = int(mel_input_length[bib].item())
                        mel = mels[bib, :, : mel_input_length[bib]]
                        s = model.predictor_encoder(mel.unsqueeze(0).unsqueeze(1))
                        ss.append(s)
                        s = model.style_encoder(mel.unsqueeze(0).unsqueeze(1))
                        gs.append(s)

                    s = torch.stack(ss).squeeze()
                    gs = torch.stack(gs).squeeze()
                    s_trg = torch.cat([s, gs], dim=-1).detach()

                    bert_dur = model.bert(texts, attention_mask=(~text_mask).int())
                    d_en = model.bert_encoder(bert_dur).transpose(-1, -2)
                    d, p = model.predictor(
                        d_en, s, input_lengths, s2s_attn_mono, text_mask
                    )
                    # get clips
                    mel_len = int(mel_input_length.min().item() / 2 - 1)
                    en = []
                    gt = []

                    p_en = []
                    wav = []

                    for bib in range(len(mel_input_length)):
                        mel_length = int(mel_input_length[bib].item() / 2)

                        random_start = np.random.randint(0, mel_length - mel_len)
                        en.append(asr[bib, :, random_start : random_start + mel_len])
                        p_en.append(p[bib, :, random_start : random_start + mel_len])

                        gt.append(
                            mels[
                                bib,
                                :,
                                (random_start * 2) : ((random_start + mel_len) * 2),
                            ]
                        )
                        y = waves[bib][
                            (random_start * 2)
                            * 300 : ((random_start + mel_len) * 2)
                            * 300
                        ]
                        wav.append(torch.from_numpy(y).to(device))

                    wav = torch.stack(wav).float().detach()

                    en = torch.stack(en)
                    p_en = torch.stack(p_en)
                    gt = torch.stack(gt).detach()
                    s = model.predictor_encoder(gt.unsqueeze(1))

                    F0_fake, N_fake = model.predictor.F0Ntrain(p_en, s)

                    loss_dur = 0
                    for _s2s_pred, _text_input, _text_length in zip(
                        d, (d_gt), input_lengths
                    ):
                        _s2s_pred = _s2s_pred[:_text_length, :]
                        _text_input = _text_input[:_text_length].long()
                        _s2s_trg = torch.zeros_like(_s2s_pred)
                        for bib in range(_s2s_trg.shape[0]):
                            _s2s_trg[bib, : _text_input[bib]] = 1
                        _dur_pred = torch.sigmoid(_s2s_pred).sum(axis=1)
                        loss_dur += F.l1_loss(
                            _dur_pred[1 : _text_length - 1],
                            _text_input[1 : _text_length - 1],
                        )

                    loss_dur /= texts.size(0)

                    s = model.style_encoder(gt.unsqueeze(1))

                    y_rec = model.decoder(en, F0_fake, N_fake, s)
                    loss_mel = stft_loss(y_rec.squeeze(), wav.detach())

                    F0_real, _, F0 = model.pitch_extractor(gt.unsqueeze(1))

                    loss_F0 = F.l1_loss(F0_real, F0_fake) / 10

                    loss_test += (loss_mel).mean()
                    loss_align += (loss_dur).mean()
                    loss_f += (loss_F0).mean()

                    iters_test += 1
                except Exception:
                    continue

        print("Epochs:", epoch + 1)
        logger.info(
            "Validation loss: %.3f, Dur loss: %.3f, F0 loss: %.3f"
            % (loss_test / iters_test, loss_align / iters_test, loss_f / iters_test)
            + "\n\n\n"
        )
        print("\n\n\n")
        writer.add_scalar("eval/mel_loss", loss_test / iters_test, epoch + 1)
        writer.add_scalar("eval/dur_loss", loss_test / iters_test, epoch + 1)
        writer.add_scalar("eval/F0_loss", loss_f / iters_test, epoch + 1)

        if (epoch + 1) % save_freq == 0:
            if (loss_test / iters_test) < best_loss:
                best_loss = loss_test / iters_test
            print("Saving..")
            state = {
                "net": {key: model[key].state_dict() for key in model},
                "optimizer": optimizer.state_dict(),
                "iters": iters,
                "val_loss": loss_test / iters_test,
                "epoch": epoch,
            }
            save_path = os.path.join(log_dir, "epoch_2nd_%05d.pth" % epoch)
            torch.save(state, save_path)

            # if estimate sigma, save the estimated simga
            if model_params.diffusion.dist.estimate_sigma_data:
                config["model_params"]["diffusion"]["dist"]["sigma_data"] = float(
                    np.mean(running_std)
                )

                with open(
                    os.path.join(log_dir, os.path.basename(config_path)), "w"
                ) as outfile:
                    yaml.dump(config, outfile, default_flow_style=True)


if __name__ == "__main__":
    main()