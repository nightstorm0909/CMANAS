import sys
sys.path.insert(0,'./')

import argparse
import json
import logging
import math
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import pickle
import random
import time
import torch
import torch.nn as nn
import torchvision.datasets as datasets
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import utils

from config_utils            import load_config, dict2config
from datasets                import get_datasets, get_nas_search_loaders
from log_utils               import AverageMeter, time_string, convert_secs2time
from procedures              import prepare_seed, prepare_logger, save_checkpoint, copy_checkpoint
from procedures              import get_optim_scheduler
from model_search            import Network
from torch.utils.data        import DataLoader
from torch.autograd          import Variable
from utils                   import get_model_infos, obtain_accuracy, create_exp_dir, model_save, get_arch_score, discretize

parser = argparse.ArgumentParser("Creating trained One Shot Model")
parser.add_argument('--alt_cifar100_split',type = int, default = 0, help = 'What alternate split to use for CIFAR100')
parser.add_argument('--batch_size',       type = int, default = 64, help = 'batch size')
parser.add_argument('--config_path',      type=str, help='The config path.')
parser.add_argument('--cutout',           action = 'store_true', default = False, help = 'use cutout')
parser.add_argument('--cutout_length',    type = int, default = 16, help = 'cutout length')
parser.add_argument('--dataset',          type = str, default = 'cifar10', help = 'dataset name')
parser.add_argument('--data_dir',         type = str, default = '../data', help = 'location of the data corpus')
parser.add_argument('--output_dir',       type = str, default = None, help = 'location of trials')
parser.add_argument('--epochs',           type = int, default = None , help = 'num of training epochs')
parser.add_argument('--gpu',              type = int, default = 0, help = 'gpu device id')
parser.add_argument('--grad_clip',        type = float, default = 5, help = 'gradient clipping')
parser.add_argument('--init_channels',    type = int, default = 16, help = 'num of init channels')
parser.add_argument('--layers',           type = int, default = 8, help = 'total number of layers')
parser.add_argument('--learning_rate',    type = float, default = 0.025, help = 'init learning rate')
parser.add_argument('--learning_rate_min',type = float, default = 0.001, help = 'min learning rate')
parser.add_argument('--model_name',       type = str, default = None, help = 'name of the trained model')
parser.add_argument('--momentum',         type = float, default = 0.9, help = 'momentum')
parser.add_argument('--pop_size',         type = int, default = None, help = 'population size')
parser.add_argument('--report_freq',      type = float, default = 100, help = 'report frequency')
parser.add_argument('--seed',             type = int, default = None, help = 'random seed')
parser.add_argument('--train_discrete',   action='store_true', default = False, help='Whether to use discretization during training')
parser.add_argument('--train_portion',    type = float, default = 0.5, help = 'portion of training data')
parser.add_argument('--valid_batch_size', type = int, default = 1024, help = 'batch size')
parser.add_argument('--warm_up',          type = int, default = 0, help = 'num of warmup epochs before the architecture search')
parser.add_argument('--weight_decay',     type = float, default = 3e-4, help = 'weight decay')
parser.add_argument('--workers',          type=int, default=2, help='number of data loading workers (default: 2)')
args = parser.parse_args()
if args.seed is None or args.seed < 0: args.seed = random.randint(1, 100000)
DIR = args.output_dir
if not os.path.exists(args.output_dir):
  create_exp_dir(DIR)

# Configuring the logger
log_format = '%(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=log_format, datefmt='%m/%d %I:%M:%S %p')
fh = logging.FileHandler(os.path.join(DIR, 'create_trainedOSM_log.txt'))
fh.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(fh)

tmp=''
for arg in sys.argv:
  tmp+=' {}'.format(arg)
logging.info(f'python{tmp}')
logging.info(f'[INFO] torch version {torch.__version__}, torchvision version: {torchvision.__version__}')
logging.info(f'[INFO] {args}')

def train_func(data_loader, model, criterion, scheduler, w_optimizer, print_freq, args, device, random, discrete=False):
  data_time, batch_time = AverageMeter(), AverageMeter()
  losses, top1, top5 = AverageMeter(), AverageMeter(), AverageMeter()
  model.train()
  end = time.time()
  for step, (inputs, targets) in enumerate(data_loader):
    #scheduler.update(None, 1.0 * step / len(data_loader))
    inputs = inputs.cuda(non_blocking=True)
    targets = targets.cuda(non_blocking=True)

    # measure data loading time
    data_time.update(time.time() - end)

    # Randomize the architecture for every training batch
    if random:
      rnd = model.random_alphas(discrete=False)
      assert model.check_alphas(rnd), "Given alphas has not been copied successfully to the model"
      if discrete:
        discrete_alphas = utils.discretize(alphas=rnd, arch_genotype=model.genotype())
        model.update_alphas(discrete_alphas)
        assert model.check_alphas(discrete_alphas), "Given alphas has not been copied successfully to the model"
        
    # update the weights
    w_optimizer.zero_grad()
    logits = model(inputs)
    loss = criterion(logits, targets)
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
    w_optimizer.step()
        
    # record
    prec1, prec5 = obtain_accuracy(logits.data, targets.data, topk=(1, 5))
    losses.update(loss.item(),  inputs.size(0))
    top1.update  (prec1.item(), inputs.size(0))
    top5.update  (prec5.item(), inputs.size(0))

    # measure elapsed time
    batch_time.update(time.time() - end)
    end = time.time()

    if step % print_freq == 0 or step + 1 == len(data_loader):
      logging.info(f'scheduler: {scheduler.get_last_lr()[0]} with random: {random}, train_discrete: {discrete}')
      Sstr = '*TRAIN* ' + time_string() + ' [{:03d}/{:03d}]'.format(step, len(data_loader))
      Tstr = 'Time {batch_time.val:.2f} ({batch_time.avg:.2f}) Data {data_time.val:.2f} ({data_time.avg:.2f})'.format(batch_time=batch_time, data_time=data_time)
      Wstr = '[Loss {loss.val:.3f} ({loss.avg:.3f}) Prec@1 {top1.val:.2f}({top1.avg:.2f}) Prec@5 {top5.val:.2f}({top5.avg:.2f})]'.format(loss=losses, top1=top1, top5=top5)
      logging.info(Sstr + ' ' + Tstr + ' ' + Wstr)
      #break
      
  return losses.avg, top1.avg, top5.avg

def main(args):
  # Reproducibility
  device = torch.device("cuda:{}".format(args.gpu))
  torch.backends.cudnn.enabled   = True
  torch.backends.cudnn.benchmark = False
  torch.backends.cudnn.deterministic = True
  prepare_seed(args.seed)
  #torch.set_default_dtype(torch.float64)
  torch.cuda.set_device(args.gpu)
  
  # Configuring dataset and dataloader
  if args.cutout:
    train_data, valid_data, xshape, num_classes = get_datasets(name = args.dataset, root = args.data_dir, cutout=args.cutout_length)
  else:
    train_data, valid_data, xshape, num_classes = get_datasets(name = args.dataset, root = args.data_dir, cutout=-1)
  
  if args.dataset == 'cifar100' and (args.alt_cifar100_split == 1):
    logging.info('[INFO] Using similar split for CIFAR100 as CIFAR10 for S1')
    with open('configs/cifar100-split.txt', 'r') as f:
      cifar100_split = json.load(f)
    cifar100_split = dict2config(cifar100_split, logger=None)
    logging.info(f'cifar100_split.train: {len(cifar100_split.train)}, cifar100_split.valid: {len(cifar100_split.valid)}')
    train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, pin_memory = True, num_workers = args.workers,
                                sampler = torch.utils.data.sampler.SubsetRandomSampler(cifar100_split.train))
    valid_loader = torch.utils.data.DataLoader(train_data, batch_size=args.valid_batch_size, pin_memory = True, num_workers = args.workers,
                                sampler = torch.utils.data.sampler.SubsetRandomSampler(cifar100_split.valid))
  elif args.dataset == 'cifar100' and (args.alt_cifar100_split == 2):
    logging.info('[INFO] Using 80-20 split for CIFAR100')
    with open('configs/cifar100_80-split.txt', 'r') as f:
      cifar100_split = json.load(f)
    cifar100_split = dict2config(cifar100_split, logger=None)
    logging.info(f'cifar100_split.train: {len(cifar100_split.train)}, cifar100_split.valid: {len(cifar100_split.valid)}')
    train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, pin_memory = True, num_workers = args.workers,
                                sampler = torch.utils.data.sampler.SubsetRandomSampler(cifar100_split.train))
    valid_loader = torch.utils.data.DataLoader(train_data, batch_size=args.valid_batch_size, pin_memory = True, num_workers = args.workers,
                                sampler = torch.utils.data.sampler.SubsetRandomSampler(cifar100_split.valid))
  else:
    logging.info('[INFO] Using same split used in NAS_BENCH_201')
    _, train_loader, valid_loader = get_nas_search_loaders(train_data=train_data, valid_data=valid_data, dataset=args.dataset,
                                                           config_root='configs', batch_size=(args.batch_size, args.valid_batch_size),
                                                           workers=args.workers)
  logging.info('train_loader length: {}, valid_loader length: {}'.format(len(train_loader), len(valid_loader)))
  
  # Model Initialization
  model = Network(args.init_channels, num_classes, args.layers, device)
  model = model.to(device)
  
  # Optimizer and criterion intialization
  config = load_config(path=args.config_path, extra={'class_num': num_classes, 'xshape': xshape}, logger=None)
  logging.info(f'config: {config}')
  optimizer, _, criterion = get_optim_scheduler(parameters=model.parameters(), config=config)
  scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer,
                                                         T_max=float(args.epochs+args.warm_up),
                                                         eta_min = args.learning_rate_min)
  criterion = criterion.cuda()
  logging.info(f'optimizer: {optimizer}\nCriterion: {criterion}')
  logging.info(f'Scheduler: {scheduler}')
  
  total_epochs = args.epochs + args.warm_up
  epoch_start = time.time()
  for epoch in range(total_epochs):
    logging.info('='*100)
    logging.info(f'[INFO] Epoch ({epoch + 1}/{total_epochs}) with LR {scheduler.get_last_lr()[0]}')

    # TRAINING ONCE ON TRAINING SET with RANDOM ARCHITECTURE VALUES
    logging.info("TRAINING")
    train_start = time.time()
    losses, top1, top5 = train_func(data_loader=train_loader,
                                    model=model,
                                    criterion=criterion,
                                    scheduler=scheduler,
                                    w_optimizer=optimizer,
                                    print_freq=args.report_freq, args=args,
                                    device=device, random=True,
                                    discrete=args.train_discrete)
    logging.info(f'[INFO] Losses: {losses:.5f}, top1: {top1:.5f}, top5: {top5:.5f} finished in {(time.time()-train_start)/60:.3f} minutes')
    scheduler.step()

    # Start architecture search after the warmup
    model_save(model, os.path.join(DIR, args.model_name))
    logging.info(f'[INFO] Epoch finished in {(time.time()-train_start) / 60:.5f} minutes')
  logging.info(f'[INFO] Training finished in {(time.time()-epoch_start) / 3600:.5f} hours')
  
if __name__ == '__main__':
  main(args)
