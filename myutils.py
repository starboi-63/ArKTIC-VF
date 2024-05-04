# from https://github.com/myungsub/CAIN/blob/master/utils.py,
# but removed the errenous normalization and quantization steps from computing the PSNR.
# Contains support for calculating/tracking accuracy and loss metrics, called in main.py and test.py

# Originally Supported:
#
# Peak Signal-to-Noise ratio (PSNR): essentially a per-pixel inverse MSE calculation.
# Ranges 0-60 where higher is better.
# great for evaluating image compression, not great for picking up on quality/blurriness.
#
# Structural Similarity Index Measure (SSIM): compares luminance, contrast, and structure of the images.
# Ranges 0-1 where 1 is best
# Sensitive to spatial shifts, rotations, distortions. Bad at picking up hue and images colors.

# Additional: VMAF
# https://github.com/Netflix/vmaf
# https://github.com/Netflix/vmaf/blob/master/resource/doc/python.md

from pytorch_msssim import ssim_matlab as calc_ssim
import math


def init_meters(loss_str):
    losses = init_losses(loss_str)
    psnrs = AverageMeter()
    ssims = AverageMeter()
    return losses, psnrs, ssims


def eval_metrics(output, ground_truth, psnrs, ssims):
    """
    Average the metrics across an interpolated frame

    output: interpolated images produced by model
    gt: ground truth (SINGLE IMAGE). What output will be compared against.
    psnrs: AverageMeter
    ssims: AverageMeter

    PSNR should be calculated for each image, since sum(log) =/= log(sum).
    """
    for b in range(ground_truth.size(0)):
        psnr = calc_psnr(output[b], ground_truth[b])
        psnrs.update(psnr)

        ssim = calc_ssim(output[b].unsqueeze(0).clamp(0,1), ground_truth[b].unsqueeze(0).clamp(0,1) , val_range=1.)
        ssims.update(ssim)



def init_losses(loss_str):
    loss_specifics = {}
    loss_list = loss_str.split('+')
    for l in loss_list:
        _, loss_type = l.split('*')
        loss_specifics[loss_type] = AverageMeter()
    loss_specifics['total'] = AverageMeter()
    return loss_specifics


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def calc_psnr(pred, gt):
    diff = (pred - gt).pow(2).mean() + 1e-8
    return -10 * math.log10(diff)


# def save_checkpoint(state, directory, is_best, filename='checkpoint.pth'):
#     """Saves checkpoint to disk"""
#     if not os.path.exists(directory):
#         os.makedirs(directory)
#     filename = os.path.join(directory, filename)
#     torch.save(state, filename)
#     if is_best:
#         shutil.copyfile(filename, os.path.join(directory, 'model_best.pth'))


# def log_tensorboard(writer, loss, psnr, ssim, lpips, lr, timestep, mode='train'):
#     writer.add_scalar('Loss/%s/%s' % mode, loss, timestep)
#     writer.add_scalar('PSNR/%s' % mode, psnr, timestep)
#     writer.add_scalar('SSIM/%s' % mode, ssim, timestep)
#     if mode == 'train':
#         writer.add_scalar('lr', lr, timestep)
