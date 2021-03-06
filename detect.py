from __future__ import division

from models import *
from utils.utils import *
from utils.datasets import *

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3 '
import sys
import time
import datetime
import argparse

from PIL import Image

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable

import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.ticker import NullLocator

#from train_light import MyModel

import cv2

def draw_bbox(model, image_folder, img_size, class_path, conf_thres, nms_thres, out_dir, train_data, use_angle, batch_size=1, n_cpu=0,):
    model.eval()  # Set in evaluation mode

    dataloader = DataLoader(
        ImageFolder(image_folder, img_size=img_size, train_data=train_data),
        batch_size=batch_size,
        shuffle=False,
        num_workers=n_cpu,
    )

    classes = load_classes(class_path)  # Extracts class labels from file

    Tensor = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

    imgs = []  # Stores image paths
    img_detections = []  # Stores detections for each image index

    print("\nPerforming object detection:")
    prev_time = time.time()

    for batch_i, (img_paths, input_imgs) in enumerate(dataloader):
        # Configure input
        input_imgs = Variable(input_imgs.type(Tensor))

        # Get detections
        with torch.no_grad():
            detections = model(input_imgs, use_angle=use_angle)
            detections = non_max_suppression(detections, conf_thres, nms_thres)

        # Log progress
        current_time = time.time()
        inference_time = datetime.timedelta(seconds=current_time - prev_time)
        prev_time = current_time
        print("\t+ Batch %d, Inference Time: %s" % (batch_i, inference_time))

        # Save image and detections
        imgs.extend(img_paths)
        img_detections.extend(detections)

        # if batch_i == 4:
        #     break

    colors = [(0,134,213), (220,0,213), (255,0,0), (255, 233, 0), (0,255,0), (0,0,255)]

    print("\nSaving images:")
    # Iterate through images and save plot of detections
    for img_i, (path, detections) in enumerate(zip(imgs, img_detections)):

        print("(%d) Image: '%s'" % (img_i, path))

        # Create plot
        #img = np.array(Image.open(path))
        img = cv2.imread(path, 1)
        plt.figure()
        fig, ax = plt.subplots(1)
        #ax.imshow(img)

        # Draw bounding boxes and labels of detections
        if detections is not None:
            # Rescale boxes to original image
            detections = rescale_boxes(detections, img_size, img.shape[:2])
            unique_labels = detections[:, -1].cpu().unique()
            n_cls_preds = len(unique_labels)
            bbox_colors = colors
            for x1, y1, x2, y2, angle, conf, cls_conf, cls_pred in detections:

                print("\t+ Label: %s, Conf: %.5f" % (classes[int(cls_pred)], cls_conf.item()))

                # New co-ordinates of the rotated bbox
                xy = rotate_detections(x1, y1, x2, y2, angle)
                pts = np.array(xy, np.int32).reshape((-1,1,2))

                #box_w = x2 - x1
                #box_h = y2 - y1

                # color = bbox_colors[int(np.where(unique_labels == int(cls_pred))[0])]
                color = bbox_colors[int(cls_pred)]
                #Draw bounding boxes
                cv2.polylines(img, [pts], isClosed=True, color=color, thickness=5)
                cv2.putText(img, classes[int(cls_pred)], (int(x1), int(y1) - 10), cv2.FONT_HERSHEY_SIMPLEX ,  2, color, cv2.LINE_AA)
                ax.imshow(img[...,::-1]) #convert BGR image to RGB image

                # # Create a Rectangle patch
                # color_mat = tuple([int(x)/255 for x in color])
                # bbox = patches.Polygon(xy, closed=True, linewidth=2, edgecolor=color_mat, facecolor="none")
                # # Add the bbox to the plot
                # ax.add_patch(bbox)
                
                # # Add label
                # plt.text(
                #     x1,
                #     y1,
                #     s=classes[int(cls_pred)],
                #     color="white",
                #     verticalalignment="top",
                #     bbox={"color": color_mat, "pad": 0},
                # )

        if detections is not None:
            # Save generated image with detections
            plt.axis("off")
            plt.gca().xaxis.set_major_locator(NullLocator())
            plt.gca().yaxis.set_major_locator(NullLocator())
            filename = path.split("/")[-1].split(".")[0]
            # os.makedirs(f'output/{out_dir}',exist_ok=True)
            # plt.savefig(f"output/{out_dir}/{filename}.png", bbox_inches="tight", pad_inches=0.0)
            plt.savefig(f"output/detection/{out_dir}.png", bbox_inches="tight", pad_inches=0.0)
        plt.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image_folder", type=str, default="/localdata/saurabh/yolov3/data/single/", help="path to dataset")
    parser.add_argument("--dataset", type=str, help='to get the respective normalization values', choices=['theodore', 'fes', 'dst'])
    parser.add_argument("--model_def", type=str, default="config/yolov3-rot-c6.cfg", help="path to model definition file")
    parser.add_argument("--pretrained_weights", type=str, default="checkpoints/dst-fes/minent33_opt.pth", help="path to weights file")
    parser.add_argument("--class_path", type=str, default="data/class.names", help="path to class label file")
    parser.add_argument("--conf_thres", type=float, default=0.5, help="object confidence threshold")
    parser.add_argument("--nms_thres", type=float, default=0.5, help="iou thresshold for non-maximum suppression")
    parser.add_argument("--batch_size", type=int, default=1, help="size of the batches")
    parser.add_argument("--n_cpu", type=int, default=0, help="number of cpu threads to use during batch generation")
    parser.add_argument("--img_size", type=int, default=416, help="size of each image dimension")
    parser.add_argument("--checkpoint_model", type=str, help="path to checkpoint model")
    parser.add_argument("--use_angle", default=False, help='set flag to train using angle')
    opt = parser.parse_args()
    print(opt)

    gpu_no = 0
    device = torch.device(f"cuda:{gpu_no}" if torch.cuda.is_available() else "cpu")
    if device.type != 'cpu':
        torch.cuda.set_device(device.index)
    print(device)

    out_dir = os.path.basename(opt.pretrained_weights).split('.')[0]
    os.makedirs("output", exist_ok=True)

    # Set up model
    model = Darknet(opt.model_def, img_size=opt.img_size).to(device)

    #model = MyModel(model, opt)

    checkpoint = torch.load(opt.pretrained_weights, map_location=lambda storage, loc:storage)
    if opt.pretrained_weights:
        if opt.pretrained_weights.endswith(".pth"):
            if opt.pretrained_weights.find('opt') != -1:
                model.load_state_dict(checkpoint['model_state_dict'])
                # optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            else:
                model.load_state_dict(checkpoint)
        else:
            model.load_darknet_weights(opt.pretrained_weights)
    #model = model.model 
    train_data = opt.dataset

    draw_bbox(model=model,
            image_folder=opt.image_folder,
            img_size=opt.img_size,
            class_path=opt.class_path,
            conf_thres=opt.conf_thres,
            nms_thres=opt.nms_thres,
            out_dir=out_dir,
            batch_size=opt.batch_size,
            n_cpu=opt.n_cpu,
            train_data=train_data,
            use_angle=opt.use_angle)

    