import time
import config
import os

import cv2
import numpy as np

from PIL import Image
import lightning as L
import torch
from torchvision import transforms

from lightning.pytorch.loggers import TensorBoardLogger
from model.artemis import ArTEMIS
from torch.optim import Adamax
from torch.optim.lr_scheduler import MultiStepLR
from loss import Loss
from metrics import eval_metrics
from data.preprocessing.vimeo90k_septuplet_process import get_loader


# Parse command line arguments
args, unparsed = config.get_args()
save_location = os.path.join(args.checkpoint_dir, "checkpoints")

# Initialize CUDA & set random seed
device = torch.device('cuda' if args.cuda else 'cpu')
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True
torch.manual_seed(args.random_seed)

if args.cuda:
    torch.cuda.manual_seed(args.random_seed)

# Initialize DataLoaders
if args.dataset == "vimeo90K_septuplet":
    t0 = time.time()
    train_loader = get_loader('train', args.data_root, args.batch_size, shuffle=True, num_workers=args.num_workers)
    t1 = time.time()
    test_loader = get_loader('test', args.data_root, args.test_batch_size, shuffle=False, num_workers=args.num_workers)
    t2 = time.time()
else:
    raise NotImplementedError


def save_image(output, gt_image, batch_index, context_frames, epoch_index):
    """
    Given an output and ground truth, save them all locally along with context frames
    outputs are, like always, a triple of ll, l, and output

    """
    _, _, output_img = output

    # context_frames is a list of 4 tensors: [(16, 3, 256, 256), (16, 3, 256, 256), (16, 3, 256, 256), (16, 3, 256, 256)]
    # Want 16 lists of lists of 4 tensors instead: [[(3, 256, 256), (3, 256, 256), (3, 256, 256), (3, 256, 256)], ...]
    context_frames = [list(context_frame) for context_frame in zip(*context_frames)]

    for sample_num, (gt, output_image, contexts) in enumerate(zip(gt_image, output_img, context_frames)):
        # Convert to numpy and scale to 0-255
        gt_image_color = gt.permute(1, 2, 0).cpu().clamp(0.0, 1.0).detach().numpy() * 255.0
        output_image_color = output_image.permute(1, 2, 0).cpu().clamp(0.0, 1.0).detach().numpy() * 255.0

        # Convert to BGR for OpenCV
        gt_image_result = cv2.cvtColor(gt_image_color.squeeze().astype(np.uint8), cv2.COLOR_RGB2BGR)
        output_image_result = cv2.cvtColor(output_image_color.squeeze().astype(np.uint8), cv2.COLOR_RGB2BGR)

        gt_image_name = f"gt_epoch{epoch_index}_batch{batch_index}_sample{sample_num}.png"
        output_image_name = f"pred_epoch{epoch_index}_batch{batch_index}_sample{sample_num}.png"

        # Create directories for each epoch, batch, sample, and frame
        gt_write_path = os.path.join(
            args.output_dir, f"epoch_{epoch_index}", f"batch_{batch_index}", f"sample_{sample_num}", gt_image_name
        )

        output_write_path = os.path.join(
            args.output_dir, f"epoch_{epoch_index}", f"batch_{batch_index}", f"sample_{sample_num}", output_image_name
        )

        # Create directories if they don't exist
        os.makedirs(os.path.dirname(gt_write_path), exist_ok=True)
        os.makedirs(os.path.dirname(output_write_path), exist_ok=True)

        # Write images to disk
        cv2.imwrite(gt_write_path, gt_image_result)
        cv2.imwrite(output_write_path, output_image_result)

        for i, context in enumerate(contexts):
            context_image_color = context.permute(1, 2, 0).cpu().clamp(0.0, 1.0).detach().numpy() * 255.0
            context_image_result = cv2.cvtColor(context_image_color.squeeze().astype(np.uint8), cv2.COLOR_RGB2BGR)
            context_image_name = f"context_epoch{epoch_index}_batch{batch_index}_sample{sample_num}_frame{i}.png"
            
            context_write_path = os.path.join(
                args.output_dir, f"epoch_{epoch_index}", f"batch_{batch_index}", f"sample_{sample_num}", context_image_name
            )

            os.makedirs(os.path.dirname(context_write_path), exist_ok=True)
            cv2.imwrite(context_write_path, context_image_result)


class ArTEMISModel(L.LightningModule):
    def __init__(self, cmd_line_args=args):
        super().__init__()
        # Call this to save command line arguments to checkpoints
        self.save_hyperparameters()
        # Initialize instance variables
        self.args = args
        self.model = ArTEMIS(num_inputs=args.nbr_frame, joinType=args.joinType, kernel_size=args.kernel_size, dilation=args.dilation)
        self.optimizer = Adamax(self.model.parameters(), lr=args.lr, betas=(args.beta1, args.beta2))
        self.loss = Loss(args)
        self.validation = eval_metrics


    def forward(self, images, output_frame_times):
        """
        Run a forward pass of the model:
        images: a list of 4 tensors, each of shape (batch_size, 3, 256, 256)
        output_frame_times: a batch of time steps: (batch_size, 1)
        """
        return self.model(images, output_frame_times)

    
    def training_step(self, batch, batch_idx):
        images, gt_image, output_frame_times = batch

        output = self(images, output_frame_times)
        loss = self.loss(output, gt_image)

        # every collection of batches, save the outputs
        if batch_idx % args.log_iter == 0:
            save_image(output, gt_image, batch_index = batch_idx, context_frames=images, epoch_index = self.current_epoch)
 
        # log metrics for each step
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        return loss

    
    def test_step(self, batch, batch_idx):
        images, gt_image, output_frame_times = batch
        output = self.model(images, output_frame_times)
        loss = self.loss(output, gt_image)
        psnr, ssim = self.validation(output, gt_image)

        # log metrics for each step
        self.log_dict({'test_loss': loss, 'psnr': psnr, 'ssim': ssim})
        
    
    def configure_optimizers(self):
        training_schedule = [40, 60, 75, 85, 95, 100]
        return {
            "optimizer": self.optimizer,
            "lr_scheduler": {
                "scheduler": MultiStepLR(optimizer = self.optimizer, milestones = training_schedule, gamma = 0.5),
            }
        }
    

""" Entry Point """

def test_and_train(args):
    torch.set_float32_matmul_precision("medium")
    logger = TensorBoardLogger(args.log_dir, name="ArTEMIS")
    model = ArTEMISModel(args)
    trainer = L.Trainer(max_epochs=args.max_epoch, log_every_n_steps=args.log_iter, logger=logger, enable_checkpointing=args.use_checkpoint)

    # Train with Lightning: Load from checkpoint if specified
    if args.use_checkpoint:
        trainer.fit(model, train_loader, ckpt_path=args.checkpoint_dir)
    else:
        trainer.fit(model, train_loader)

    # Test the model with Lightning
    trainer.test(model, test_loader)


def single_interpolation(args):
    """
    Run an interpolation on a single input of 4 frames: 
    Produces a single output frame at an arbitrary time step
    """
    img_transforms = transforms.Compose([
        transforms.ToTensor()
    ])

    t = args.time_step
    input_image_paths = [args.f0_path, args.f1_path, args.f2_path, args.f3_path]
    input_images = [torch.unsqueeze(img_transforms(Image.open(path)), 0) for path in input_image_paths]
    output_frame_times = torch.tensor([t])
    model = ArTEMISModel.load_from_checkpoint(args.parameter_path)
    model.eval()
    _, _, out = model.forward(input_images, output_frame_times)



def main(args):
    if args.eval:
        single_interpolation(args)
    else:
        test_and_train(args)


if __name__ == "__main__":
    main(args)
