from __future__ import division

from models import *
from utils.logger import *
from utils.utils import *
from utils.datasets import *
from utils.parse_config import *
from test import evaluate
from detect import draw_bbox

from terminaltables import AsciiTable

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3,4,5,6'
import sys
import time
import datetime
import argparse

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision import transforms
from torch.autograd import Variable
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler

def adjust_learning_rate(optimizer, epoch):
    # use warmup
    if epoch < 5:
        lr = opt.lr * ((epoch + 1) / 5)
    else:
    # use cosine lr
        PI = 3.14159
        lr = opt.lr * 0.5 * (1 + math.cos(epoch * PI / opt.epochs)) 
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
        print(lr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=1000, help="number of epochs")
    parser.add_argument("--lr", type=float, default=5e-4, help="learning rate")
    parser.add_argument("--batch_size", type=int, default=10, help="size of each image batch")
    parser.add_argument("--gradient_accumulations", type=int, default=2, help="number of gradient accums before step")
    parser.add_argument("--model_def", type=str, default="config/yolov3-rot-c1.cfg", help="path to model definition file")
    parser.add_argument("--data_config", type=str, default="config/coco.data", help="path to data config file")
    parser.add_argument("--pretrained_weights", type=str, default="weights/darknet53.conv.74", help="if specified starts from checkpoint model")
    parser.add_argument("--n_cpu", type=int, default=6, help="number of cpu threads to use during batch generation")
    parser.add_argument("--img_size", type=int, default=416, help="size of each image dimension")
    parser.add_argument("--checkpoint_interval", type=int, default=1, help="interval between saving model weights")
    parser.add_argument("--evaluation_interval", type=int, default=1, help="interval evaluations on validation set")
    parser.add_argument("--compute_map", default=False, help="if True computes mAP every tenth batch")
    parser.add_argument("--multiscale_training", default=False, help="allow for multi-scale training")
    parser.add_argument("--use_angle", default=False, help='set flag to train using angle')
    opt = parser.parse_args()
    print(opt)

    logger = Logger("logs")
    gpu_no = 6
    device = torch.device(f"cuda:{gpu_no}" if torch.cuda.is_available() else "cpu")
    torch.cuda.set_device(device.index)
    print(device)

    os.makedirs("output", exist_ok=True)
    os.makedirs("checkpoints", exist_ok=True)

    # Get data configuration
    data_config = parse_data_config(opt.data_config)
    train_path = data_config["train"]
    valid_path = data_config["valid"]
    train_annpath = data_config["json_train"]
    valid_annpath = data_config["json_val"]
    class_names = load_classes(data_config["names"])

    if train_path.find('custom') != -1:   ### flag to use same mean and std values for evaluation as well
        train_dataset = 'theodore'
        print('Training on Theodore Dataset')
    elif train_path.find('fes') != -1:
        train_dataset = 'fes'
        print('Training on FES dataset')
    elif train_path.find('DST') != -1:
        train_dataset = 'dst'
        print('Training on DST dataset')
    elif train_path.find('coco') != -1:
        train_dataset = 'coco'
        print('Training on COCO dataset')
    elif train_path.find('cepdof') != -1:
        train_dataset = 'cepdof_light'
        print('Training on CEPDOF dataset')
    elif train_path.find('mwr') != -1:
        train_dataset = 'mwr'
        print('Training on MWR dataset')
    else:
        raise FileNotFoundError('Invalid Dataset')

    if len(class_names) == 80:    ### To indicate it is not coco dataset
        class_80 = True
    else:
        class_80 = False

    # Initiate model
    model = Darknet(opt.model_def).to(device)
    model.apply(weights_init_normal)

    # If specified we start from checkpoint
    
    if opt.pretrained_weights:
        if opt.pretrained_weights.endswith(".pth"):
            checkpoint = torch.load(opt.pretrained_weights, map_location=f'cuda:{device.index}')
            if opt.pretrained_weights.find('opt') != -1:
                model.load_state_dict(checkpoint['model_state_dict'])
            else:
                model.load_state_dict(checkpoint)
        else:
            model.load_darknet_weights(opt.pretrained_weights)

    # # Get dataloader
    # dataset = ImageAnnotation(folder_path=train_path, json_path=train_annpath, img_size=opt.img_size, augment=True, multiscale=opt.multiscale_training, class_80=class_80)
    # dataloader = torch.utils.data.DataLoader(
    #     dataset,
    #     batch_size=opt.batch_size,
    #     shuffle=True,
    #     num_workers=opt.n_cpu,
    #     pin_memory=True,
    #     collate_fn=dataset.collate_fn,
    # )

    # Get dataloader
    dataset = ListDataset(train_path, augment=True, multiscale=opt.multiscale_training, normalized_labels=False, pixel_norm=True, train_data=train_dataset)
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=opt.batch_size,
        shuffle=True,
        num_workers=opt.n_cpu,
        pin_memory=True,
        collate_fn=dataset.collate_fn,
    )

    #optimizer = torch.optim.SGD(model.parameters(), lr=opt.lr, momentum=0.9, weight_decay=0.0005)
    optimizer = torch.optim.Adam(model.parameters(), lr=opt.lr)
    #### Load optimizer state dict if available
    if opt.pretrained_weights.find('opt') != -1:
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    ##### Use lr scheduler to drop lr after desired number of epochs
    #scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[300,350,400], gamma=0.5)

    metrics = [
        "grid_size",
        "loss",
        "x",
        "y",
        "w",
        "h",
        "angle",
        "conf",
        "cls",
        "cls_acc",
        "recall50",
        "recall75",
        "precision",
        "conf_obj",
        "conf_noobj",
    ]

    for epoch in range(opt.epochs):
        ### Use lr_scheduler
        #adjust_learning_rate(optimizer,epoch)

        model.train()
        start_time = time.time()
        train_acc_epoch = 0
        train_loss_epoch = 0
        for batch_i, (_, imgs, targets) in enumerate(dataloader):
            batches_done = len(dataloader) * epoch + batch_i

            imgs = Variable(imgs.to(device))
            targets = Variable(targets.to(device), requires_grad=False)

            loss, outputs = model(imgs, targets=targets, use_angle=opt.use_angle)
            loss.backward()

            if batches_done % opt.gradient_accumulations:
                # Accumulates gradient before each step
                optimizer.step()
                optimizer.zero_grad()
                print(optimizer.param_groups[0]["lr"])

            # ----------------
            #   Log progress
            # ----------------

            log_str = "\n---- [Epoch %d/%d, Batch %d/%d] ----\n" % (epoch, opt.epochs, batch_i, len(dataloader))

            metric_table = [["Metrics", *[f"YOLO Layer {i}" for i in range(len(model.yolo_layers))]]]

            # Log metrics at each YOLO layer
            for i, metric in enumerate(metrics):
                formats = {m: "%.6f" for m in metrics}
                formats["grid_size"] = "%2d"
                formats["cls_acc"] = "%.2f%%"
                row_metrics = [formats[metric] % yolo.metrics.get(metric, 0) for yolo in model.yolo_layers]
                metric_table += [[metric, *row_metrics]]

            # Tensorboard logging
            tensorboard_log = []
            batch_acc = 0
            for j, yolo in enumerate(model.yolo_layers):
                for name, metric in yolo.metrics.items():
                    if name != "grid_size":
                        tensorboard_log += [(f"{name}_{j+1}", metric)]
                        if name == "cls_acc":
                            batch_acc += metric

            batch_acc = batch_acc / 3
            tensorboard_log += [("loss", loss.item())]
            tensorboard_log += [("accu", batch_acc)]

            logger.list_of_scalars_summary(tensorboard_log, batches_done)

            # Accumulate loss for every batch of epoch
            train_acc_epoch += batch_acc
            train_loss_epoch += loss.item()

            log_str += AsciiTable(metric_table).table
            log_str += f"\nTotal loss {loss.item()}"
            log_str += f"\nTotal accu {batch_acc}"

            # Determine approximate time left for epoch
            epoch_batches_left = len(dataloader) - (batch_i + 1)
            time_left = datetime.timedelta(seconds=epoch_batches_left * (time.time() - start_time) / (batch_i + 1))
            log_str += f"\n---- ETA {time_left}"

            print(log_str)

            model.seen += imgs.size(0)

            # if batch_i == 100:
            #     break

        #scheduler.step()
        # Calculate loss for each epoch
        train_acc_epoch = train_acc_epoch / (batch_i+1)
        train_loss_epoch = train_loss_epoch / (batch_i+1)

        # Logging values to Tensorboard
        logger.scalar_summary("epoch_acc", train_acc_epoch, epoch)
        logger.scalar_summary("epoch_loss", train_loss_epoch, epoch)

        # Print trainin loss and accuracy for each epoch
        print(f'Training Accuracy for Epoch {epoch}: {train_acc_epoch}')
        print(f'Training Loss for Epoch {epoch}: {train_loss_epoch}')

        if epoch % opt.checkpoint_interval == 0:
            model.eval()
            torch.save({
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'epoch': epoch,
                    'loss':  loss,
                    },f"checkpoints/yolov3_ckpt_opt_{train_dataset}_%d.pth" % epoch)

        if epoch % opt.evaluation_interval == 0:
            if epoch >= 0:
                print("\n---- Evaluating Model ----")
                # Evaluate the model on the validation set
                precision, recall, AP, f1, ap_class, val_acc, val_loss = evaluate(
                    model,
                    path=valid_path,
                    json_path=valid_annpath,
                    iou_thres=0.5,
                    conf_thres=0.5,
                    nms_thres=0.5,
                    img_size=opt.img_size,
                    batch_size=opt.batch_size,
                    class_80=class_80,
                    gpu_num=gpu_no,
                    train_data= train_dataset,
                    use_angle=opt.use_angle
                )
                evaluation_metrics = [
                    ("val_precision", precision.mean()),
                    ("val_recall", recall.mean()),
                    ("val_mAP", AP.mean()),
                    ("val_f1", f1.mean()),
                ]
                logger.val_list_of_scalars_summary(evaluation_metrics, epoch)
                logger.val_scalar_summary("epoch_acc", val_acc, epoch)
                logger.val_scalar_summary("epoch_loss", val_loss, epoch)
                # Print class APs and mAP
                ap_table = [["Index", "Class name", "AP"]]
                for i, c in enumerate(ap_class):
                    ap_table += [[c, class_names[c], "%.5f" % AP[i]]]
                print(AsciiTable(ap_table).table)
                print(f"---- mAP {AP.mean()}")

            #model.save_darknet_weights(f"checkpoints/darknet_ckpt_%d.pth" % epoch)

                # #Save image detections
                # draw_bbox(model=model,
                #         image_folder=valid_path,
                #         img_size=opt.img_size,
                #         class_path=data_config["names"],
                #         conf_thres=0.8,
                #         nms_thres=0.8,
                #         n_cpu=opt.n_cpu,
                #         out_dir='training')
