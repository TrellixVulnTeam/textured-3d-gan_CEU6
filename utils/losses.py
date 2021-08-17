import torch
import torch.nn as nn
import torch.nn.functional as F

def loss_flat(mesh, norms):
    """
    Smoothness regularizer.
    Encourages neighboring faces to have similar normals (low cosine distance).
    """
    loss  = 0.
    for i in range(3):
        norm1 = norms
        norm2 = norms[:, mesh.ff[:, i]]
        cos = torch.sum(norm1 * norm2, dim=-1)
        loss += torch.mean((cos - 1) ** 2) 
    loss *= (mesh.faces.shape[0]/2.)
    return loss

def mean_iou_noreduce(alpha_pred, alpha_real):
    alpha_pred = alpha_pred > 0.5
    alpha_real = alpha_real > 0.5
    intersection = (alpha_pred & alpha_real).float().sum(dim=[-1, -2])
    union = (alpha_pred | alpha_real).float().sum(dim=[-1, -2])
    iou = intersection / (union + 1e-6)
    return iou

def mean_miou_noreduce(pred, target, balanced=True):
    intersection = torch.min(pred, target).sum(dim=[-1, -2])
    union = torch.max(pred, target).sum(dim=[-1, -2])

    if balanced:
        iou = intersection / (union + 1e-6)
        wts = target.sum(dim=[-1, -2]) > 1e-6 # Compute mean over non-empty channels
        return (iou*wts).sum(dim=-1) / wts.sum(dim=-1)
    else:
        iou = intersection.sum(dim=-1) / (union.sum(dim=-1) + 1e-6)
        return iou
    
def agreement_score(iou_or_miou, rotations, temperature=0.01):
    v_conf = F.softmax(iou_or_miou / temperature, dim=1)
    D = 1 - (rotations @ rotations.transpose(1, 2))**2
    v_conf_mask = v_conf.unsqueeze(-1) * v_conf.unsqueeze(-2)
    v_agr = (D * v_conf_mask).sum(dim=[1, 2])
    return v_agr


def geodesic_distance(predicted_rotations, target_rotations, return_count=False):
    assert predicted_rotations.shape == target_rotations.shape
    Rdist = 1 - (predicted_rotations * target_rotations).sum(dim=-1)**2
    mask = target_rotations[:, 0] != -1000 # Select images whose ground-truth pose is available
    count = mask.sum().item()
    if count == 0:
        if return_count:
            return 0, 0
        else:
            return 0
    Rdist = Rdist[mask]
    gd = Rdist.mean().item()
    if return_count:
        return gd, count
    else:
        return gd
    
def evaluate_geodesic_distance(experiment_name, predicted_rotations, target_rotations, num_total=None):
    mask = target_rotations[:, 0] != -1000 # Select images whose ground-truth pose is available
    mask_len = mask.sum().item()
    gd = geodesic_distance(predicted_rotations, target_rotations)
    if num_total is not None:
        r = len(predicted_rotations) / num_total
        recall_str = f' - Recall: {r:.5f} ({len(predicted_rotations)}/{num_total})'
    else:
        recall_str = ''
    print(f'[{experiment_name}] {gd:.5f} (evaluated on {mask_len} matching images out of {len(mask)}){recall_str}')


class LaplacianLoss(nn.Module):
    def __init__(self, laplacian, average=False):
        super().__init__()
        self.average = average
        self.register_buffer('laplacian', laplacian)

    def forward(self, x):
        batch_size = x.size(0)
        x = torch.matmul(self.laplacian, x)
        dims = tuple(range(x.ndimension())[1:])
        x = x.pow(2).sum(dims)
        if self.average:
            return x.sum() / batch_size
        else:
            return x

# This class was borrowed from the pix2pix(HD) / SPADE repo,
# and has been modified to add support for output masking and weighting
class GANLoss(nn.Module):
    def __init__(self, gan_mode, target_real_label=1.0, target_fake_label=0.0,
                 tensor=torch.FloatTensor, opt=None):
        super().__init__()
        self.real_label = target_real_label
        self.fake_label = target_fake_label
        self.real_label_tensor = None
        self.fake_label_tensor = None
        self.zero_tensor = None
        self.Tensor = tensor
        self.gan_mode = gan_mode
        self.opt = opt
        if gan_mode == 'ls':
            pass
        elif gan_mode == 'original':
            pass
        elif gan_mode == 'w':
            pass
        elif gan_mode == 'hinge':
            pass
        else:
            raise ValueError('Unexpected gan_mode {}'.format(gan_mode))

    def get_target_tensor(self, input, target_is_real):
        if target_is_real:
            if self.real_label_tensor is None:
                self.real_label_tensor = self.Tensor(1).fill_(self.real_label)
                self.real_label_tensor.requires_grad_(False)
            return self.real_label_tensor.expand_as(input)
        else:
            if self.fake_label_tensor is None:
                self.fake_label_tensor = self.Tensor(1).fill_(self.fake_label)
                self.fake_label_tensor.requires_grad_(False)
            return self.fake_label_tensor.expand_as(input)

    def get_zero_tensor(self, input):
        if self.zero_tensor is None:
            self.zero_tensor = self.Tensor(1).fill_(0)
            self.zero_tensor.requires_grad_(False)
        return self.zero_tensor.expand_as(input)

    def mean(self, x, mask=None, weight=None):
        if weight is None:
            weight = 1
            
        if mask is None:
            return torch.mean(x) * weight
        else:
            assert x.shape == mask.shape, (x.shape, mask.shape)
            ret = torch.sum(x * mask, dim=[1, 2, 3]) / torch.sum(mask, dim=[1, 2, 3])
            return torch.mean(ret) * weight
    
    def loss(self, input, target_is_real, for_discriminator=True, mask=None, weight=None):
        if self.gan_mode == 'original':  # cross entropy loss
            target_tensor = self.get_target_tensor(input, target_is_real)
            loss = F.binary_cross_entropy_with_logits(input, target_tensor)
            return loss
        elif self.gan_mode == 'ls':
            target_tensor = self.get_target_tensor(input, target_is_real)
            return F.mse_loss(input, target_tensor)
        elif self.gan_mode == 'hinge':
            if for_discriminator:
                if target_is_real:
                    minval = torch.min(input - 1, self.get_zero_tensor(input))
                    loss = -self.mean(minval, mask, weight)
                else:
                    minval = torch.min(-input - 1, self.get_zero_tensor(input))
                    loss = -self.mean(minval, mask, weight)
            else:
                assert target_is_real, "The generator's hinge loss must be aiming for real"
                loss = -self.mean(input, mask, weight)
            return loss
        else:
            # wgan
            if target_is_real:
                return -input.mean()
            else:
                return input.mean()

    def __call__(self, input, target_is_real, for_discriminator=True, mask=None, weight=None):
        if isinstance(input, list):
            if mask is not None:
                assert isinstance(mask, list)
                assert len(input) == len(mask)
            loss = 0
            for idx, pred_i in enumerate(input):
                if isinstance(pred_i, list):
                    pred_i = pred_i[-1]
                loss_tensor = self.loss(pred_i, target_is_real, for_discriminator,
                                        mask[idx] if mask is not None else None,
                                        weight[idx] if weight is not None else None)
                bs = 1 if len(loss_tensor.size()) == 0 else loss_tensor.size(0)
                new_loss = torch.mean(loss_tensor.view(bs, -1), dim=1)
                loss += new_loss
            if weight is None:
                return loss / len(input)
            else:
                return loss / sum(weight)
        else:
            return self.loss(input, target_is_real, for_discriminator, mask)
