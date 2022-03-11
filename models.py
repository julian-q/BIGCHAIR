from collections import OrderedDict
from typing import Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch_geometric.nn import GraphSAGE, GCNConv, GAT, GATConv, global_mean_pool, dense_diff_pool
from torch_geometric.data import Data
from transformers import AutoTokenizer, AutoModel, CLIPProcessor, Trainer, TrainingArguments

from layers import BatchZERON_GCN, BatchGCNMax

class SimpleMeshEncoder(nn.Module):
	def __init__(self, joint_embed_dim, opt="GraphSAGE"):
		super(SimpleMeshEncoder, self).__init__()
		if opt == "GraphSAGE":
			self.message_passing = GraphSAGE(in_channels=3,
										 	hidden_channels=joint_embed_dim // 2,
										 	num_layers=3,
										 	out_channels=joint_embed_dim)
		elif opt == "GAT":
			self.message_passing = GAT(in_channels=3,
											 hidden_channels=joint_embed_dim // 2,
											 num_layers=3,
											 out_channels=joint_embed_dim)
		self.reduce = global_mean_pool

	def forward(self, batch):
		x = self.message_passing(x=batch.x, edge_index=batch.edge_index)
		x = self.reduce(x=x, batch=batch.batch)
		return x

class HierarchicalMeshEncoder(nn.Module):
	def __init__(self, input_dim):
		super(HierarchicalMeshEncoder, self).__init__()

		self.conv1_embed = GAT(in_channels=input_dim, hidden_channels=64, heads=4)
		self.conv1_pool = GCNConv(in_channels=64, out_channels=32)

		self.conv2_embed = GAT(in_channels=32, hidden_channes=32, heads=4)
		self.conv2_pool = GCNConv(in_channels=32, out_channels=16)

		self.conv3_embed = GAT(in_channels=16, hidden_channels=16, heads=4)

		self.lin1 = nn.Linear(8, 8)

	def forward(self, batch):
		x = self.conv1_embed(x=batch.x, edge_index=batch.edge_index)
		s = self.conv1_pool(x=x, edge_index=batch.edge_index)

		x, adj, l1, e1 = dense_diff_pool(x=x, adj=batch.edge_index, s=s)

		x = self.conv2_embed(x=x, edge_index=batch.edge_index)
		s = self.conv2_pool(x=x, edge_index=batch.edge_index)

		x, adj, l2, e2 = dense_diff_pool(x=x, adj=batch.edge_index, s=s)

		x = self.conv3_embed(x=x, edge_index=batch.edge_index)

		x = self.lin1(x)
		x = global_mean_pool(x=x, batch=batch.batch)
		return x


class BatchMeshEncoder(nn.Module):
	def __init__(self, joint_embed_dim):
		super(BatchMeshEncoder, self).__init__()
		self.h1 = BatchZERON_GCN(3, 60)
		self.h21 = BatchZERON_GCN(60, 60)
		self.h22 = BatchZERON_GCN(60, 60)
		self.h23 = BatchZERON_GCN(60, 60)
		self.h24 = BatchZERON_GCN(60,120)
		self.h3 = BatchZERON_GCN(120, 120)
		self.h4 = BatchZERON_GCN(120, 120)
		self.h41 = BatchZERON_GCN(120, 150)
		self.h5 = BatchZERON_GCN(150, 200)
		self.h6 = BatchZERON_GCN(200, 210)
		self.h7 = BatchZERON_GCN(210, 250)
		self.h8 = BatchZERON_GCN(250, 300)
		self.h81 = BatchZERON_GCN(300, 300)
		self.h9 = BatchZERON_GCN(300, 300)
		self.h10 = BatchZERON_GCN(300, 300)
		self.h11 = BatchZERON_GCN(300, 300)
		self.reduce = BatchGCNMax(300,joint_embed_dim)

	def resnet(self, features, res):
		temp = features[:,:res.shape[1]]
		temp = temp + res
		features = torch.cat((temp,features[:,res.shape[1]:]), dim = 1)
		return features, features

	def forward(self, mesh, play = False):
		positions, adj = mesh
		# print positions[:5, :5]
		res = positions
		features = self.h1(positions, adj, F.elu)
		features = self.h21(features, adj, F.elu)
		features = self.h22(features, adj, F.elu)
		features = self.h23(features, adj, F.elu)
		features = self.h24(features, adj, F.elu)
		features = self.h3(features, adj, F.elu)
		features = self.h4(features, adj, F.elu)
		features = self.h41(features, adj, F.elu)
		features = self.h5(features, adj, F.elu)
		features = self.h6(features, adj, F.elu)
		features = self.h7(features, adj, F.elu)
		features = self.h8(features, adj, F.elu)
		features = self.h81(features, adj, F.elu)
		features = self.h9(features, adj, F.elu)
		features = self.h10(features, adj, F.elu)
		features = self.h11(features, adj, F.elu)
		latent 	 = self.reduce(features , adj, F.elu)

		return latent

class LayerNorm(nn.LayerNorm):
	"""Subclass torch's LayerNorm to handle fp16."""

	def forward(self, x: torch.Tensor):
		orig_type = x.dtype
		ret = super().forward(x.type(torch.float32))
		return ret.type(orig_type)

class QuickGELU(nn.Module):
	def forward(self, x: torch.Tensor):
		return x * torch.sigmoid(1.702 * x)

class ResidualAttentionBlock(nn.Module):
	def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None):
		super().__init__()

		self.attn = nn.MultiheadAttention(d_model, n_head)
		self.ln_1 = LayerNorm(d_model)
		self.mlp = nn.Sequential(OrderedDict([
			("c_fc", nn.Linear(d_model, d_model * 4)),
			("gelu", QuickGELU()),
			("c_proj", nn.Linear(d_model * 4, d_model))
		]))
		self.ln_2 = LayerNorm(d_model)
		self.attn_mask = attn_mask

	def attention(self, x: torch.Tensor):
		self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
		return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

	def forward(self, x: torch.Tensor):
		x = x + self.attention(self.ln_1(x))
		x = x + self.mlp(self.ln_2(x))
		return x

class Transformer(nn.Module):
	def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None):
		super().__init__()
		self.width = width
		self.layers = layers
		self.resblocks = nn.Sequential(*[ResidualAttentionBlock(width, heads, attn_mask) for _ in range(layers)])

	def forward(self, x: torch.Tensor):
		return self.resblocks(x)

class CLIP_pretrained(nn.Module):
	def __init__(self,
				 joint_embed_dim: int,
				 mesh_encoder: nn.Module,
				 context_length: int,
				 opt
				 ):
		super().__init__()

		self.joint_embed_dim = joint_embed_dim
		self.mesh_encoder = mesh_encoder(joint_embed_dim, opt=opt)
		self.mesh_encoder.train()
		self.text_encoder = AutoModel.from_pretrained('openai/clip-vit-base-patch32').text_model
		self.tokenizer = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32', mode_max_length=77).tokenizer
		self.text_projection = nn.Linear(512, joint_embed_dim)
		self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

	def encode_mesh(self, mesh):
		return self.mesh_encoder(mesh)

	def encode_text(self, text):
		x = self.text_encoder(text).last_hidden_state
		# x = self.text_projection(x[torch.arange(x.shape[0]), text.argmax(dim=-1)])
		x = self.text_projection(torch.sum(x, dim=1))
		return x

	def forward(self, batched_meshes, text, desc2mesh):
		mesh_features = self.encode_mesh(batched_meshes)

		# mesh_features = torch.eye(10, self.joint_embed_dim)
		text_features = self.encode_text(text)
		# text_features = torch.zeros(text.shape[0], self.joint_embed_dim).to(torch.float)
		# text_features[torch.arange(text.shape[0]), desc2mesh] = 1

		# normalized features
		mesh_features = mesh_features / mesh_features.norm(dim=-1, keepdim=True)
		text_features = text_features / text_features.norm(dim=-1, keepdim=True)

		# cosine similarity as logits
		logit_scale = self.logit_scale.exp()
		logits_per_image = logit_scale * mesh_features @ text_features.t()
		logits_per_text = logits_per_image.t()

		# shape = [global_batch_size, global_batch_size]
		return logits_per_image, logits_per_text


class CLIP(nn.Module):
	def __init__(self,
				 joint_embed_dim: int,
				 # mesh,
				 mesh_encoder: nn.Module,
				 # text
				 context_length: int,
				 vocab_size: int,
				 transformer_width: int,
				 transformer_heads: int,
				 transformer_layers: int
				 ):
		super().__init__()

		self.context_length = context_length

		self.mesh_encoder = mesh_encoder(joint_embed_dim)

		# self.transformer = TextEncoder(
		#     vocab_size=vocab_size,
		#     embedding_dim=transformer_width,
		#     hidden_dim=transformer_width,
		#     output_dim=joint_embed_dim
		# )

		# self.transformer = Transformer(
		# 	width=transformer_width,
		# 	layers=transformer_layers,
		# 	heads=transformer_heads,
		# 	attn_mask=self.build_attention_mask()
		# )

		self.tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name_or_path="bert-base-uncased",
													   model_max_length=context_length)
		self.transformer = AutoModel.from_pretrained("bert-base-uncased")
		pretrained_out_dim = 768
		self.text_linear = nn.Linear(in_features=pretrained_out_dim, out_features=joint_embed_dim)
		self.vocab_size = vocab_size
		# self.token_embedding = nn.Embedding(vocab_size, transformer_width)
		# self.positional_embedding = nn.Parameter(torch.empty(self.context_length, transformer_width))
		# self.ln_final = LayerNorm(transformer_width)

		# self.text_projection = nn.Parameter(torch.empty(transformer_width, joint_embed_dim))
		self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

		# self.initialize_parameters()

	# def initialize_parameters(self):
	# 	nn.init.normal_(self.token_embedding.weight, std=0.02)
	# 	nn.init.normal_(self.positional_embedding, std=0.01)
	#
	# 	proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
	# 	attn_std = self.transformer.width ** -0.5
	# 	fc_std = (2 * self.transformer.width) ** -0.5
	# 	for block in self.transformer.resblocks:
	# 		nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
	# 		nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
	# 		nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
	# 		nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)
	#
	# 	if self.text_projection is not None:
	# 		nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)

	def build_attention_mask(self):
		# lazily create causal attention mask, with full attention between the vision tokens
		# pytorch uses additive attention mask; fill with -inf
		mask = torch.empty(self.context_length, self.context_length)
		mask.fill_(float("-inf"))
		mask.triu_(1)  # zero out the lower diagonal
		return mask

	@property
	def dtype(self):
		return self.visual.conv1.weight.dtype

	def encode_mesh(self, mesh):
		return self.mesh_encoder(mesh)

	def encode_text(self, text):
		# x = self.token_embedding(text)  # [batch_size, n_ctx, d_model]
		#
		# x = x + self.positional_embedding
		# x = x.permute(1, 0, 2)  # NLD -> LND
		# x = self.transformer(x)
		# x = x.permute(1, 0, 2)  # LND -> NLD
		# x = self.ln_final(x)
		#
		# # x.shape = [batch_size, n_ctx, transformer.width]
		# # take features from the eot embedding (eot_token is the highest number in each sequence)
		# x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection

		x = self.transformer(text, return_dict=False)[0] # index 0 of tuple is last hidden state
		x = x[torch.arange(x.shape[0]), (text == 102).nonzero()[:, 1]]
		x = self.text_linear(x)
		return x

	def forward(self, mesh, text):
		mesh_features = self.encode_mesh(mesh)
		text_features = self.encode_text(text)

		# normalized features
		mesh_features = mesh_features / mesh_features.norm(dim=-1, keepdim=True)
		text_features = text_features / text_features.norm(dim=-1, keepdim=True)

		# cosine similarity as logits
		logit_scale = self.logit_scale.exp()
		logits_per_image = logit_scale * mesh_features @ text_features.t()
		logits_per_text = logits_per_image.t()

		# shape = [global_batch_size, global_batch_size]
		return logits_per_image, logits_per_text


def convert_weights(model: nn.Module):
	"""Convert applicable model parameters to fp16"""

	def _convert_weights_to_fp16(l):
		if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
			l.weight.data = l.weight.data.half()
			if l.bias is not None:
				l.bias.data = l.bias.data.half()

		if isinstance(l, nn.MultiheadAttention):
			for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
				tensor = getattr(l, attr)
				if tensor is not None:
					tensor.data = tensor.data.half()

		for name in ["text_projection", "proj"]:
			if hasattr(l, name):
				attr = getattr(l, name)
				if attr is not None:
					attr.data = attr.data.half()

	model.apply(_convert_weights_to_fp16)


def build_model(state_dict: dict):
	joint_embed_dim = state_dict["text_projection"].shape[1]
	context_length = state_dict["positional_embedding"].shape[0]
	vocab_size = state_dict["token_embedding.weight"].shape[0]
	transformer_width = state_dict["ln_final.weight"].shape[0]
	transformer_heads = transformer_width // 64
	transformer_layers = len(set(k.split(".")[2] for k in state_dict if k.startswith(f"transformer.resblocks")))

	model = CLIP(
		joint_embed_dim,
		context_length, vocab_size, transformer_width, transformer_heads, transformer_layers
	)

	for key in ["input_resolution", "context_length", "vocab_size"]:
		if key in state_dict:
			del state_dict[key]

	convert_weights(model)
	model.load_state_dict(state_dict)
	return model.eval()

## baseline text encoder
class TextEncoder(nn.Module):

	def __init__(self, vocab_size, embedding_dim, hidden_dim, output_dim):
		super().__init__()
		self.embedding_dim = embedding_dim
		self.hidden_dim = hidden_dim
		self.output_dim = output_dim
		self.vocab_size = vocab_size

		self.embeddings = nn.Embedding(num_embeddings=vocab_size, embedding_dim=embedding_dim, padding_idx=0)
		self.embeddings.weight.data.uniform_(-1, 1)

		self.rnn = nn.GRU(input_size=hidden_dim, hidden_size=hidden_dim, batch_first=True)
		self.fc1 = nn.Sequential(
			nn.Linear(hidden_dim, hidden_dim),
			nn.ReLU()
		)
		self.fc2 = nn.Sequential(
			nn.Linear(hidden_dim, output_dim),
			nn.ReLU()
		)


	def compute_sequence_lengths(self, input_batch):
		"""
		input_batch: torch tensor of input indicies
			size = B x L_max batch, B is batch size, L_max is max caption lengths,
			Wrapped in a variable,  0 means padding
			a non-zero positive value indicates a word index
		return:
			seq_lengths: 1d tensor of size B representing the length of each caption in
			in the current batch
		"""
		seq_lengths = torch.gt(input_batch, 0).sum(dim=1)
		seq_lengths = seq_lengths.long()
		return seq_lengths

	def forward(self, input_batch):
		"""
		:param
		input_batch: torch tensor of input indicies
			size = B x L_max batch, B is batch size, L_max is max caption lengths,
			Wrapped in a variable,  0 means padding
			a non-zero positive value indicates a word index
		:return:
		result tensor after RNN and 2 FC layers, size B x O, B is batch size and O is output dim (aka joint_embed_dim)
		"""
		embedding_batch = self.embeddings(input_batch)
		seq_lengths = self.compute_sequence_lengths(input_batch)

		packed_embedding = torch.nn.utils.rnn.pack_padded_sequence(embedding_batch, seq_lengths, batch_first=True)
		_, last_hidden = self.rnn(packed_embedding)
		last_hidden = last_hidden.squeeze(0)  # B x H

		res = self.fc1(last_hidden)
		res = self.fc2(res)
		return res


