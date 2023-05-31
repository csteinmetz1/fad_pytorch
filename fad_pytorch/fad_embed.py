# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/02_fad_embed.ipynb.

# %% auto 0
__all__ = ['GUDGUD_LICENSE', 'setup_embedder', 'embed_all', 'main']

# %% ../nbs/02_fad_embed.ipynb 5
import os
import argparse
import laion_clap 
from laion_clap.training.data import get_audio_features
from accelerate import Accelerator
import warnings
import torch

from aeiou.core import get_device, load_audio, get_audio_filenames, makedir
from aeiou.datasets import AudioDataset
from aeiou.hpc import HostPrinter
from torch.utils.data import DataLoader
from pathlib import Path

try:
    from fad_pytorch.pann import Cnn14_16k
except: 
    from pann import Cnn14_16k

# %% ../nbs/02_fad_embed.ipynb 6
def setup_embedder(
        model_choice='clap', # 'clap' | 'vggish' | 'pann'
        device='cuda',
        accelerator=None,
    ):
    "load the embedder model"
    embedder = None
    
    if model_choice == 'clap':
        clap_fusion, clap_amodel = True, "HTSAT-base"
        #doesn't work:  warnings.filterwarnings('ignore')  # temporarily disable CLAP warnings as they are super annoying. 
        clap_module = laion_clap.CLAP_Module(enable_fusion=clap_fusion, device=device, amodel=clap_amodel).requires_grad_(False).eval()
        clap_ckpt_path = os.getenv('CLAP_CKPT')  # you'll need access to this .ckpt file
        if clap_ckpt_path is not None:
            #print(f"Loading CLAP from {clap_ckpt_path}")
            clap_module.load_ckpt(ckpt=clap_ckpt_path, verbose=False)
        else:
            if accelerator is None or accelerator.is_main_process: print("No CLAP checkpoint specified, going with default") 
            clap_module = laion_clap.CLAP_Module(enable_fusion=False)
            clap_module.load_ckpt(model_id=1, verbose=False)
        #warnings.filterwarnings("default")   # turn warnings back on. 
        embedder = clap_module # synonyms 
        sample_rate = 48000
        
    # next two model loading codes from gudgud96's repo: https://github.com/gudgud96/frechet-audio-distance, LICENSE below
    elif model_choice == "vggish":   # https://arxiv.org/abs/1609.09430
        embedder = torch.hub.load('harritaylor/torchvggish', 'vggish')
        use_pca=False
        use_activation=False
        if not use_pca:  embedder.postprocess = False
        if not use_activation: embedder.embeddings = torch.nn.Sequential(*list(embedder.embeddings.children())[:-1])
        sample_rate = 16000

    elif model_choice == "pann": # https://arxiv.org/abs/1912.10211
        model_path = os.path.join(torch.hub.get_dir(), "Cnn14_16k_mAP%3D0.438.pth")
        if not(os.path.exists(model_path)):
            torch.hub.download_url_to_file('https://zenodo.org/record/3987831/files/Cnn14_16k_mAP%3D0.438.pth', model_path)
        embedder = Cnn14_16k(sample_rate=16000, window_size=512, hop_size=160, mel_bins=64, fmin=50, fmax=8000, classes_num=527)
        checkpoint = torch.load(model_path, map_location=device)
        embedder.load_state_dict(checkpoint['model'])
        sample_rate = 16000

    else:
        raise ValueError("Sorry, other models not supported yet")
        
    embedder.eval()   
    return embedder, sample_rate


GUDGUD_LICENSE = """
MIT License

Copyright (c) 2022 Hao Hao Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# %% ../nbs/02_fad_embed.ipynb 8
def embed_all(args): 
    model_choice, real_path, fake_path, chunk_size, sr, max_batch_size = args.embed_model, args.real_path, args.fake_path, args.chunk_size, args.sr, args.batch_size
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    ddps = f"[{local_rank}/{world_size}]"  # string for distributed computing info, e.g. "[1/8]" 

    accelerator = Accelerator()
    hprint = HostPrinter(accelerator)  # hprint only prints on head node
    device = accelerator.device    # get_device()
    hprint(f"{ddps} args = {args}")
    hprint(f'{ddps} Using device: {device}')
    
 
    """ # let accelerate split up the files among processsors
    # get the list(s) of audio files
    real_filenames = get_audio_filenames(real_path)
    #hprint(f"{ddps} real_path, real_filenames = {real_path}, {real_filenames}")
    fake_filenames = get_audio_filenames(fake_path)
    minlen = len(real_filenames)
    if len(real_filenames) != len(fake_filenames):
        hprint(f"{ddps} WARNING: len(real_filenames)=={len(real_filenames)} != len(fake_filenames)=={len(fake_filenames)}. Truncating to shorter list") 
        minlen = min( len(real_filenames) , len(fake_filenames) )
    
    # subdivide file lists by process number
    num_per_proc = minlen // world_size
    start = local_rank * num_per_proc
    end =  minlen if local_rank == world_size-1 else (local_rank+1) * num_per_proc
    #print(f"{ddps} start, end = ",start,end) 
    real_filenames, fake_filenames = real_filenames[start:end], fake_filenames[start:end]
    """

    # setup embedder and dataloader
    embedder, emb_sample_rate = setup_embedder(model_choice, device, accelerator)
    if sr != emb_sample_rate:
        hprint(f"\n*******\nWARNING: sr={sr} != {model_choice}'s emb_sample_rate={emb_sample_rate}. Will resample audio to the latter\n*******\n")
        sr = emb_sample_rate
    hprint(f"{ddps} Embedder '{model_choice}' ready to go!")

    real_dataset = AudioDataset(real_path, augs='Stereo(), PhaseFlipper()', sample_rate=emb_sample_rate, sample_size=chunk_size, return_dict=True, verbose=args.verbose)
    fake_dataset = AudioDataset(fake_path, augs='Stereo(), PhaseFlipper()', sample_rate=emb_sample_rate, sample_size=chunk_size, return_dict=True, verbose=args.verbose)
    batch_size = min( len(real_dataset) // world_size , max_batch_size ) 
    hprint(f"\nGiven max_batch_size = {max_batch_size}, len(real_dataset) = {len(real_dataset)}, and world_size = {world_size}, we'll use batch_size = {batch_size}")
    real_dl = DataLoader(real_dataset, batch_size=batch_size, shuffle=False)
    fake_dl = DataLoader(fake_dataset, batch_size=batch_size, shuffle=False)
    
    real_dl, fake_dl, embedder = accelerator.prepare( real_dl, fake_dl, embedder )  # prepare handles distributing things among GPUs
    
    # note that we don't actually care if real & fake files are pulled in the same order; we'll only be comparing the *distributions* of the data.
    with torch.no_grad():
        for dl, name in zip([real_dl, fake_dl],['real','fake']):
            newdir_already = False
            for i, data_dict in enumerate(dl):
                audio, filename_batch = data_dict['inputs'], data_dict['filename']
                if not newdir_already: 
                    p = Path( filename_batch[0] )
                    dir_already = True
                    newdir = f"{p.parents[0]}_emb_{model_choice}"
                    hprint(f"newdir = {newdir}")
                    makedir(newdir) 

                #print(f"{ddps} i = {i}/{len(real_dataset)}, filename = {filename_batch[0]}")
                audio = audio.to(device)


                if model_choice == 'clap': 
                    while len(audio.shape) < 3: 
                        audio = audio.unsqueeze(0) # add batch and/or channel dims 
                    embeddings = embedder.get_audio_embedding_from_data(audio.mean(dim=1).to(device), use_tensor=True).to(audio.dtype)

                elif model_choice == "vggish":
                    audio = torch.mean(audio, dim=1)   # vggish requries we convert to mono
                    embeddings = []                    # ...whoa, vggish can't even handle batches?  we have to pass 'em through singly?
                    for bi, waveform in enumerate(audio): 
                        e = embedder.forward(waveform.cpu().numpy(), emb_sample_rate)
                        embeddings.append(e) 
                    embeddings = torch.cat(embeddings, dim=0)

                elif model_choice == "pann": 
                    audio = torch.mean(audio, dim=1)  # mono only.  todo:  keepdim=True ?
                    out = embedder.forward(audio, None)
                    embeddings = out['embedding'].data

                hprint(f"embeddings.shape = {embeddings.shape}")
                # TODO: for now we'll just dump each batch on each proc to its own file; this could be improved
                outfilename = f"{newdir}/emb_p{local_rank}_b{i}.pt"
                print(f"{ddps} Saving embeddings to {outfilename}")
                torch.save(embeddings.cpu().detach(), outfilename)
    return        

# %% ../nbs/02_fad_embed.ipynb 9
def main(): 
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('embed_model', help='chioce of embedding model: clap | vggish | pann', default='clap')
    parser.add_argument('real_path', help='Path of files of real audio', default='real/')
    parser.add_argument('fake_path', help='Path of files of fake audio', default='fake/')
    parser.add_argument('--chunk_size', type=int, default=24000, help='Length of chunks (in audio samples) to embed')
    parser.add_argument('--batch_size', type=int, default=64, help='MAXIMUM Batch size for computing embeddings (may go smaller)')
    parser.add_argument('--sr', type=int, default=48000, help='sample rate (will resample inputs at this rate)')
    parser.add_argument('--verbose', action='store_true',  help='Show notices of resampling when reading files')

    args = parser.parse_args()
    embed_all(args)

# %% ../nbs/02_fad_embed.ipynb 10
if __name__ == '__main__' and "get_ipython" not in dir():
    main()
