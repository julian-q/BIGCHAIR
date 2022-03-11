import torch
from torch import nn
from torch import optim
from dataset_pyg import AnnotatedMeshDataset
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from models import SimpleMeshEncoder, DescriptionEncoder, ContrastiveLearner
from loss import ContrastiveLoss
from grad_cache import GradCache
import random
from argparse import ArgumentParser
torch.autograd.set_detect_anomaly(True)

argp = ArgumentParser()
argp.add_argument('--gnn',
	help='Which gnn to run ("GraphSAGE" or "GAT")',
	choices=['GraphSAGE', 'GAT'], default='GAT')
argp.add_argument('--epoch',
	help='number of epochs', type=int, default=32)
argp.add_argument('--batch_size',
	help='batch size', type=int, default=100)
argp.add_argument('--sub_batch_size',
	help='batch size', type=int, default=2)
argp.add_argument('--descs_per_mesh',
	help='number of descriptions per each mesh in a batch', type=int, default=5)
argp.add_argument('--joint_embedding_dim',
	help='dimension of joint embedding space', type=int, default=128)
argp.add_argument('--parameters_file',
	help='name of file to save parameters to', default='model_params.pt')
argp.add_argument('--loss_file',
	help='name of file to save training loss to', default='losses.pt')
argp.add_argument('--batch_accu_file',
	help='name of file to save per batch accuracy', default='accs.pt')
args = argp.parse_args()

# dataset setup
dataset_root = './dataset/'
dataset = AnnotatedMeshDataset(dataset_root)
dataset.shuffle()
train_share = int(len(dataset) * 0.8)
val_share = (len(dataset) - train_share) // 2
train_dataset = dataset[:train_share]
val_dataset = dataset[train_share:train_share + val_share]
test_dataset = dataset[train_share + val_share:]
train_dataloader = DataLoader(train_dataset, batch_size=args.sub_batch_size)

device = 'cuda:0' if torch.cuda.is_available() else 'cpu'

# init models
desc_encoder = DescriptionEncoder(args.joint_embedding_dim).to(device)
mesh_encoder = SimpleMeshEncoder(args.joint_embedding_dim).to(device)
contrastive_loss = ContrastiveLoss().to(device)

def split_inputs(model_input, chunk_size):
	if isinstance(model_input, torch.Tensor):
			return list(model_input.split(chunk_size, dim=0))
	elif isinstance(model_input, list) and all(isinstance(x, Data) for x in model_input):
		return model_input
	else:
		raise NotImplementedError

# gradient caching
gc = GradCache(models=[desc_encoder, mesh_encoder],
			   chunk_sizes=[args.sub_batch_size * args.descs_per_mesh,
			   				args.sub_batch_size],
			   loss_fn=contrastive_loss,
			   split_input_fn=split_inputs)

desc_encoder.train()
mesh_encoder.train()
contrastive_loss.train()
parameters = list(desc_encoder.parameters()) \
		   + list(mesh_encoder.parameters()) \
		   + list(contrastive_loss.parameters())
optimizer = optim.Adam(parameters, lr=5e-5,betas=(0.9,0.98),eps=1e-6,weight_decay=0.2) 
# Params used from paper, the lr is smaller, more safe for fine tuning to new dataset

losses = []
train_accs = []
val_accs = []

for epoch in range(args.epoch):
	print('starting epoch', epoch)
	batch = []
	i_batch = 0
	for sub_batch in train_dataloader:
		batch.append(sub_batch)

		if len(batch) == args.batch_size // args.sub_batch_size:
			optimizer.zero_grad()
		
			batch_descs, batch_meshes = [sub_batch.descs for sub_batch in batch], batch
			# since each mesh may have differing numbers of descriptions, we sample a fixed
			# number (self.descs_per_mesh) of them for each mesh in order to standardize
			# memory usage
			sampled_descs = [[random.choices(descs, k=args.descs_per_mesh) for descs in sub_batch_descs]
							 for sub_batch_descs in batch_descs]
			tokenized_descs = torch.cat([desc_encoder.tokenize(sub_batch_descs) 
										 for sub_batch_descs in sampled_descs],
										 dim=0)
			loss = gc(tokenized_descs, batch_meshes) # GradCache takes care of backprop
			print(loss.item())
			losses.append(loss)
			optimizer.step()

			batch = []







