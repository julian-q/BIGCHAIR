# based on https://github.com/openai/CLIP/issues/83
import torch
from torch import nn
from torch import optim
from dataset_pyg import AnnotatedMeshDataset
from torch_geometric.loader import DataLoader
from models import CLIP_pretrained, SimpleMeshEncoder
from clip import tokenize

BATCH_SIZE = 15
EPOCH = 32

dataset_root = './dataset/'
# assumes that ./dataset/raw/ is full of .obj files!!!
dataset = AnnotatedMeshDataset(dataset_root)
train_dataloader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)

device = "cuda:0" if torch.cuda.is_available() else "cpu"

model = CLIP_pretrained(joint_embed_dim=128, 
                        mesh_encoder=SimpleMeshEncoder, 
                        context_length=dataset.max_desc_length).to(device)
model.train()

loss_mesh = nn.CrossEntropyLoss()
loss_text = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=5e-5,betas=(0.9,0.98),eps=1e-6,weight_decay=0.2) # Params used from paper, the lr is smaller, more safe for fine tuning to new dataset

for epoch in range(EPOCH):
    print('starting epoch', epoch)
    for i_batch, batch in enumerate(train_dataloader):
        optimizer.zero_grad()

        # now, batch contains a mega graph containing each
        # graph in the batch, as usual with pyg data loaders.
        # each of these graphs has a 'descs' array containing
        # its descriptions, all of which get combined into one
        # giant nested array. we tokenize them below:
        batch.to(device)
        # batch_texts = torch.cat([tokenize(model_descs, context_length=dataset.max_desc_length + 2)
        #                         for model_descs in batch.descs],
        #                         dim=0).to(device)
        
        batch_texts = torch.cat([model.tokenizer(model_descs, return_tensors="pt", padding='max_length', 
                                                truncation=True).input_ids
                                 for model_descs in batch.descs], dim=0).to(device)

        logits_per_mesh, logits_per_text = model(batch, batch_texts)
        # uniform distribution over matching descs
        target_per_mesh = torch.zeros(BATCH_SIZE, batch_texts.shape[0]).to(device) 
        # one-hot distribution for single matching shape
        target_per_text = torch.zeros(batch_texts.shape[0], BATCH_SIZE).to(device) 
        i_desc = 0
        for i_mesh, model_descs in enumerate(batch['descs']):
            target_per_mesh[i_mesh, i_desc:i_desc + len(model_descs)] = 1 / len(model_descs)
            target_per_text[i_desc:i_desc + len(model_descs), i_mesh] = 1
            i_desc += len(model_descs)

        total_loss = (loss_mesh(logits_per_mesh, target_per_mesh) + loss_text(logits_per_text, target_per_text)) / 2
        print('batch', i_batch, 'loss:', total_loss.item())
        total_loss.backward()
        optimizer.step()