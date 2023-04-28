import os
import hydra
import random
import numpy as np
import multiprocessing
from omegaconf import DictConfig

from datasets.caption.field import TextField
from datasets.caption.coco import build_coco_dataloaders
from datasets.caption.metrics import PTBTokenizer, Cider
from models.caption import Transformer
from models.caption.detector import build_detector
from tools.extract_features import extract_vis_features
from utils.cap_scheduler import CosineLRScheduler

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter
from engine.caption_engine import *
from vicap_dataset import *


def main(gpu, config):
    # dist init
    torch.backends.cudnn.enabled = False
    rank = config.exp.rank * config.exp.ngpus_per_node + gpu
    dist.init_process_group('nccl', 'env://', rank=rank, world_size=config.exp.world_size)

    torch.manual_seed(config.exp.seed)
    np.random.seed(config.exp.seed)
    random.seed(config.exp.seed)

    device = torch.device(f"cuda:{gpu}")
    torch.cuda.set_device(gpu)

    # extract features
    detector = build_detector(config).to(device)
    detector.load_state_dict(torch.load(config.model.detector.checkpoint)['model'], strict=False)

    model = Transformer(detector=detector, config=config)
    model = model.to(device)

    start_epoch = 0
    best_cider_val = 0.0
    best_cider_test = 0.0

    model = DDP(model, device_ids=[gpu], find_unused_parameters=True, broadcast_buffers=False)
    optimizers = build_optimizers(model, config, mode='xe')

    # tensorboard:
    writer = SummaryWriter(log_dir='tensorboard') if rank == 0 or rank == 1 else None

    # dataloaders

    text_field = TextField(vocab_path=config.dataset.vocab_path)
    train_dataset = dataloaders['train'].dataset
    cider = Cider(PTBTokenizer.tokenize([e.text for e in train_dataset.examples]))
    tokenizer = multiprocessing.Pool(8)  #config.optimizer.num_workers)

    fr_xe_epochs = config.optimizer.freezing_xe_epochs  # 10
    fr_sc_epochs = fr_xe_epochs + config.optimizer.freezing_sc_epochs  # 15
    ft_xe_epochs = fr_sc_epochs + config.optimizer.finetune_xe_epochs  # 20
    ft_sc_epochs = ft_xe_epochs + config.optimizer.finetune_sc_epochs  # 20
    total_epochs = ft_sc_epochs

    for epoch in range(max(0, start_epoch), total_epochs):
        phase = 'ft_xe'
        train_res = train_xe(
            model,
            dataloaders,
            optimizers=optimizers,
            text_field=text_field,
            epoch=epoch,
            rank=rank,
            config=config,
            scheduler=scheduler,
            writer=writer,
        )
        samplers['train'].set_epoch(epoch)

        if rank == 0:
            best_cider_val = evaluate_metrics(
                model,
                optimizers,
                dataloader=dataloaders['valid_dict'],
                text_field=text_field,
                epoch=epoch,
                split='valid',
                config=config,
                train_res=train_res,
                writer=writer,
                best_cider=best_cider_val,
                which=phase,
                scheduler=scheduler,
            )

        if rank == 1:
            best_cider_test = evaluate_metrics(
                model,
                optimizers,
                dataloader=dataloaders['test_dict'],
                text_field=text_field,
                epoch=epoch,
                split='test',
                config=config,
                train_res=train_res,
                writer=writer,
                best_cider=best_cider_test,
                which=phase,
                scheduler=scheduler,
            )

        if rank == 0:
            save_checkpoint(
                model,
                optimizers,
                epoch=epoch,
                scores=[],
                best_ciders=[0, 0],
                config=config,
                filename=f'checkpoint_{phase}.pth',
                scheduler=scheduler,
            )
            if epoch >= 15:
                save_checkpoint(
                    model,
                    optimizers,
                    epoch=epoch,
                    scores=[],
                    best_ciders=[0, 0],
                    config=config,
                    filename=f'checkpoint_{epoch}.pth',
                    scheduler=scheduler,
                )

        torch.distributed.barrier()


@hydra.main(config_path="configs/caption", config_name="coco_config")
def run_main(config: DictConfig) -> None:
    mp.spawn(main, nprocs=config.exp.ngpus_per_node, args=(config,))


if __name__ == "__main__":
    # os.environ["DATA_ROOT"] = "/home/quang/datasets/coco_caption"
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "6688"
    run_main()