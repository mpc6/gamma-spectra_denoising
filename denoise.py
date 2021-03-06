import os
import sys
import time
import h5py
import pickle
import argparse
import numpy as np

import matplotlib
if not os.environ.get('DISPLAY', '').strip():
    matplotlib.use('Agg')
import matplotlib.pyplot as plt

import torch
from torch.utils.data import DataLoader, TensorDataset
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau

from load_data import load_data
from build_dataset import save_dataset
from spectra_utils import compare_results
from model import DnCNN, DnCNN_Res
#from utils import weights_init_kaiming

from sklearn.model_selection import train_test_split
from tqdm import tqdm
#from tensorboardX import SummaryWriter
#from torchvision.utils import make_grid
from skimage.metrics import peak_signal_noise_ratio as psnr

        
def setup_gpus():
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    device_ids = [i for i in range(torch.cuda.device_count())]
    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(map(str, device_ids))
    return device_ids

def psnr_of_batch(clean_imgs, denoised_imgs):
    batch_psnr = 0
    for i in range(clean_imgs.shape[0]):
        batch_psnr += psnr(clean_imgs[i,:], denoised_imgs[i,:], data_range=1)
    return batch_psnr/clean_imgs.shape[0]

def main():
    start = time.time()

    parser = argparse. ArgumentParser(description='Gamma-Spectra Denoising Trainer')
    parser.add_argument('--dettype', type=str, default='HPGe', help='detector type to train {HPGe, NaI, CZT}')
    parser.add_argument('--test_set', type=str, default='data/training.h5', help='h5 file with training vectors')
    parser.add_argument('--all', default=False, help='denoise all examples in test_set file', action='store_true')
    parser.add_argument('--batch_size', type=int, default=64, help='batch size for denoising')
    parser.add_argument('--seed', type=int, help='random seed')
    parser.add_argument('--model', type=str, default='models/best_model.pt', help='location of model to use')
    parser.add_argument('--outdir', type=str, help='location to save output plots')
    parser.add_argument('--outfile', type=str, help='location to save output data', default='denoised.h5')
    parser.add_argument('--savefigs', help='saves plots of results', default=False, action='store_true')
    args = parser.parse_args()

    # if output directory is not provided, save plots to model directory
    if not args.outdir:
        args.outdir = os.path.dirname(args.model)
    else:
        # make sure output dirs exists
        os.makedirs(args.outdir, exist_ok=True)
       
    # make sure data files exist
    assert os.path.exists(args.test_set), f'Cannot find testset vectors file {args.test_set}'


    # detect gpus and setup environment variables
    device_ids = setup_gpus()
    print(f'Cuda devices found: {[torch.cuda.get_device_name(i) for i in device_ids]}')

    print('Loading datasets')
    test_data = load_data(args.test_set, args.dettype.upper())
    noisy_spectra = test_data['noisy_spectrum']
    clean_spectra = test_data['spectrum']
    spectra_keV = test_data['keV']

    noisy_spectra = np.expand_dims(noisy_spectra, axis=1)
    clean_spectra = np.expand_dims(clean_spectra, axis=1)

    assert noisy_spectra.shape == clean_spectra.shape, 'Mismatch between shapes of training and target data'

    # load parameters for model
    params = pickle.load(open(args.model.replace('.pt','.npy'),'rb'))['model']

    train_mean = params['train_mean'] 
    train_std = params['train_std'] 

    if not args.seed:
        args.seed = params['train_seed']

    # applying random seed for reproducability
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # create dataset for denoising, if not 'all' use training seed to recreate validation set
    if not args.all:
        _, x_val, _, y_val = train_test_split(noisy_spectra, clean_spectra, test_size = 0.1, random_state=args.seed)
        val_dataset = TensorDataset(torch.Tensor(x_val), torch.Tensor(y_val))
    else:
        val_dataset = TensorDataset(torch.Tensor(noisy_spectra), torch.Tensor(clean_spectra))

    print(f'Number of examples to denoise: {len(val_dataset)}')

    # create batched data loaders for model
    val_loader = DataLoader(dataset=val_dataset, num_workers=os.cpu_count(), batch_size=args.batch_size, shuffle=False)
    print(f'Number of batches {len(val_loader)}')

    # create and load model
    if params['model_name'] == 'DnCNN':
        model = DnCNN(num_channels=params['num_channels'], num_layers=params['num_layers'], \
                      kernel_size=params['kernel_size'], stride=params['stride'], num_filters=params['num_filters']) 
    elif params['model_name'] == 'DnCNN-res':
        model = DnCNN_Res(num_channels=params['num_channels'], num_layers=params['num_layers'], \
                      kernel_size=params['kernel_size'], stride=params['stride'], num_filters=params['num_filters']) 
    else:
        print(f'Model name {params["model_name"]} is not supported.')
        return 1

    # prepare model for data parallelism (use multiple GPUs)
    model = torch.nn.DataParallel(model, device_ids=device_ids).cuda()

    # loaded saved model
    print(f'Loading weights for {params["model_name"]} model from {args.model} for {params["model_type"]}')
    model.load_state_dict(torch.load(args.model))

    # Main training loop

    print(f'Denoising spectra')
    model.eval() 
    total_psnr_noisy = 0
    total_psnr_denoised = 0

    denoised = []
    with torch.no_grad():
        for num, (noisy_spectra, clean_spectra) in enumerate(val_loader, start=1):

            # move batch to GPU
            noisy_spectra = Variable(noisy_spectra.cuda())
            clean_spectra = Variable(clean_spectra.cuda())

            # make predictions
            preds = model((noisy_spectra-train_mean)/train_std)

            # calculate PSNR 
            clean_spectra = clean_spectra.cpu().numpy().astype(np.float32)
            noisy_spectra = noisy_spectra.cpu().numpy().astype(np.float32)
            preds = preds.cpu().numpy().astype(np.float32)
            psnr_noisy = psnr_of_batch(clean_spectra, noisy_spectra)

            # save denoised spectrum
            if params['model_type'] == 'Gen-spectrum':
                denoised_spectrum = preds
            else:
                denoised_spectrum = noisy_spectra-preds 

            # add batch of denoised spectra to list of denoised spectra
            denoised.extend(denoised_spectrum.tolist()) 

            psnr_denoised = psnr_of_batch(clean_spectra, denoised_spectrum)
            total_psnr_noisy += psnr_noisy
            total_psnr_denoised += psnr_denoised
            print(f'[{num}/{len(val_loader)}] PSNR {psnr_noisy} --> {psnr_denoised}, increase of {psnr_denoised-psnr_noisy}')
            if args.savefigs:
                compare_results(spectra_keV, clean_spectra[0,0,:], noisy_spectra[0,0,:], preds[0,0,:], args.outdir, str(num))

    # save denoised data to file, currently only supports entire dataset
    if args.all:
        assert len(test_data['noisy_spectrum']) == len(denoised), f'{len(test_data["noisy_spectrum"])} examples yet {len(denoised)} denoised' 
        denoised = np.squeeze(np.array(denoised))
        test_data['noisy_spectrum'] = denoised 
        outfile = os.path.join(args.outdir, args.outfile)
        print(f'Saving denoised spectrum to {outfile}')
        save_dataset(args.dettype.upper(), test_data, outfile)

    avg_psnr_noisy = total_psnr_noisy/len(val_loader)
    avg_psnr_denoised = total_psnr_denoised/len(val_loader)

    print(f'Average PSNR: {avg_psnr_denoised}, average increase of {avg_psnr_denoised-avg_psnr_noisy}')

    print(f'Script completed in {time.time()-start:.2f} secs')
    return 0

if __name__ == '__main__':
    sys.exit(main())

