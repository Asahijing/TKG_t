import torch
import torch.nn as nn
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack

import onqg.dataset.Constants as Constants

from onqg.models.modules.Attention import GatedSelfAttention, ConcatAttention
from onqg.models.modules.Layers import GraphEncoderLayer, SparseGraphEncoderLayer

# from pytorch_pretrained_bert import BertModel


class RNNEncoder(nn.Module):
    """
    Input: (1) inputs['src_seq']
           (2) inputs['lengths'] 
           (3) inputs['feat_seqs']
    Output: (1) enc_output
            (2) hidden
    """
    def __init__(self, n_vocab, d_word_vec, d_model, n_layer,
                 brnn, rnn, feat_vocab, d_feat_vec, slf_attn, 
                 dropout):
        self.name = 'rnn'

        self.n_layer = n_layer
        self.num_directions = 2 if brnn else 1
        assert d_model % self.num_directions == 0, "d_model = hidden_size x direction_num"
        self.hidden_size = d_model // self.num_directions
        self.d_enc_model = d_model

        super(RNNEncoder, self).__init__()

        self.word_emb = nn.Embedding(n_vocab, d_word_vec, padding_idx=Constants.PAD)
        input_size = d_word_vec

        self.feature = False if not feat_vocab else True
        if self.feature:
            self.feat_embs = nn.ModuleList([
                nn.Embedding(n_f_vocab, d_feat_vec, padding_idx=Constants.PAD) for n_f_vocab in feat_vocab
            ])
            input_size += len(feat_vocab) * d_feat_vec
        
        self.slf_attn = slf_attn
        if slf_attn:
            self.gated_slf_attn = GatedSelfAttention(d_model)
        
        if rnn == 'lstm':
            self.rnn = nn.LSTM(input_size, self.hidden_size, num_layers=n_layer,
                               dropout=dropout, bidirectional=brnn, batch_first=True)
        elif rnn == 'gru':
            self.rnn = nn.GRU(input_size, self.hidden_size, num_layers=n_layer,
                              dropout=dropout, bidirectional=brnn, batch_first=True)
        else:
            raise ValueError("Only support 'LSTM' and 'GRU' for RNN-based Encoder ")

    @classmethod
    def from_opt(cls, opt):
        return cls(opt['n_vocab'], opt['d_word_vec'], opt['d_model'], opt['n_layer'],
                   opt['brnn'], opt['rnn'], opt['feat_vocab'], opt['d_feat_vec'], 
                   opt['slf_attn'], opt['dropout'])

    def forward(self, inputs, is_con = False):
        src_seq, lengths, feat_seqs = inputs['src_seq'], inputs['lengths'], inputs['feat_seqs']
        if is_con:
            src_seq, lengths = inputs['con_seq'], inputs['con_lengths']
        lengths = torch.LongTensor(lengths.data.view(-1).tolist())
        
        enc_input = self.word_emb(src_seq)
        if self.feature:
            feat_outputs = [feat_emb(feat_seq) for feat_seq, feat_emb in zip(feat_seqs, self.feat_embs)]
            feat_outputs = torch.cat(feat_outputs, dim=2)
            enc_input = torch.cat((enc_input, feat_outputs), dim=-1)
        
        enc_input = pack(enc_input, lengths, batch_first=True, enforce_sorted=False)
        enc_output, hidden = self.rnn(enc_input, None)
        enc_output = unpack(enc_output, batch_first=True)[0]

        if self.slf_attn:
            mask = (src_seq == Constants.PAD).byte()
            enc_output = self.gated_slf_attn(enc_output, mask)

        # try:
        #     mask = (src_seq == Constants.PAD).byte()
        #     mask = mask.unsqueeze(2).repeat(1, 1, 300).float()
        #     hidden = torch.sum(enc_input * mask, dim=1)
        #     enc_output = enc_input
        #     denominator = lengths.unsqueeze(1).repeat(1, 300).float().to(hidden.device)
        #     hidden = hidden / denominator
        # except:
        #     print(enc_input.size(), mask.size(), length.size())
        
        return enc_output, hidden


class GraphEncoder(nn.Module):
    """Combine GGNN (Gated Graph Neural Network) and GAT (Graph Attention Network)
    Input: (1) nodes - [batch_size, node_num, d_model]
           (2) edges - ([batch_size, node_num * node_num], [batch_size, node_num * node_num]) 1st-inlink, 2nd-outlink
           (3) mask - ([batch_size, node_num, node_num], [batch_size, node_num, node_num]) 1st-inlink, 2nd-outlink
           (4) node_feats - list of [batch_size, node_num]
    """
    def __init__(self, n_edge_type, d_model, n_layer, alpha, d_feat_vec,
                 feat_vocab, layer_attn, dropout, attn_dropout):
        self.name = 'graph'
        super(GraphEncoder, self).__init__()
        self.layer_attn = layer_attn

        self.hidden_size = d_model
        self.d_model = d_model
        ###=== node features ===###
        self.feature = True if feat_vocab else False
        if self.feature:
            self.feat_embs = nn.ModuleList([
                nn.Embedding(n_f_vocab, d_feat_vec, padding_idx=Constants.PAD) for n_f_vocab in feat_vocab
            ])
            #self.hidden_size += d_feat_vec * len(feat_vocab)  
            self.feature_transform = nn.Linear(self.hidden_size + d_feat_vec * len(feat_vocab), self.hidden_size)
        ###=== edge embedding ===###
        # self.edge_in_emb = nn.Embedding(n_edge_type, self.hidden_size * d_model, padding_idx=Constants.PAD)
        # self.edge_out_emb = nn.Embedding(n_edge_type, self.hidden_size * d_model, padding_idx=Constants.PAD)
        # self.edge_bias = edge_bias
        # if edge_bias:
        #     self.edge_in_emb_bias = nn.Embedding(n_edge_type, d_model, padding_idx=Constants.PAD)
        #     self.edge_out_emb_bias = nn.Embedding(n_edge_type, d_model, padding_idx=Constants.PAD)
        ###=== graph encode layers===###
        self.layer_stack = nn.ModuleList([
            GraphEncoderLayer(self.hidden_size, d_model, alpha, feature=self.feature,
                              dropout=dropout, attn_dropout=attn_dropout) for _ in range(n_layer)
        ])
        ###=== gated output ===###
        self.gate = nn.Linear(2 * d_model, d_model, bias=False)

    @classmethod
    def from_opt(cls, opt):
        return cls(opt['n_edge_type'], opt['d_model'], opt['n_layer'], opt['alpha'], 
                   opt['d_feat_vec'], opt['feat_vocab'], opt['layer_attn'], 
                   opt['dropout'], opt['attn_dropout'])
    
    def gated_output(self, outputs, inputs):
        concatenation = torch.cat((outputs, inputs), dim=3)
        g_t = torch.sigmoid(self.gate(concatenation))

        output = g_t * outputs + (1 - g_t) * inputs
        return output

    def forward(self, inputs):
        
        nodes, mask = inputs['nodes'], inputs['mask']
        #node_feats, node_type = inputs['feat_seqs'], inputs['type']
        nodes = self.activate(nodes)
        node_output = nodes    # batch_size x cross_num x node_num x d_model
        ###=== get embeddings ===###
        feat_hidden = None

        if self.feature:
            feat_hidden = [feat_emb(node_feat) for node_feat, feat_emb in zip(node_feats, self.feat_embs)]
            feat_hidden = torch.cat(feat_hidden, dim=2)     # batch_size x node_num x (hidden_size - d_model)
            node_output = self.feature_transform(torch.cat((node_output, feat_hidden), dim=-1))
        # batch_size x (node_num * node_num) x hidden_size x d_model
        # edge_in_hidden = self.edge_in_emb(edges[0]).view(nodes.size(0), -1, self.hidden_size, nodes.size(2))
        # edge_out_hidden = self.edge_out_emb(edges[1]).view(nodes.size(0), -1, self.hidden_size, nodes.size(2))
        # edge_hidden = (edge_in_hidden, edge_out_hidden)
        # if self.edge_bias:
        #     # batch_size x (node_num * node_num) x d_model
        #     edge_in_hidden_bias, edge_out_hidden_bias = self.edge_in_emb_bias(edges[0]), self.edge_out_emb_bias(edges[1])
        # edge_hidden_bias = (edge_in_hidden_bias, edge_out_hidden_bias) if self.edge_bias else None
        ##=== forward ===###
        node_outputs = []
        
        for enc_layer in self.layer_stack:
            # node_output = enc_layer(node_output, edge_hidden, mask, feat_hidden=feat_hidden,
            #                         edge_hidden_bias=edge_hidden_bias)
            node_output = enc_layer(node_output, mask, feat_hidden=feat_hidden)
            node_outputs.append(node_output)
        node_output = self.gated_output(node_output, nodes)
        node_outputs[-1] = node_output

        hidden = [layer_output.transpose(0, 1)[0] for layer_output in node_outputs]
        
        if self.layer_attn:
            node_output = node_outputs
        
        return node_output, hidden


class SparseGraphEncoder(nn.Module):
    """Sparse version of Graph Encoder"""
    """Combine GGNN (Gated Graph Neural Network) and GAT (Graph Attention Network)
    Input: (1) nodes - [batch_size, node_num, d_model]
           (2) edges - [edge_type_num]
           (3) mask - ([batch_size, node_num, node_num], [batch_size, node_num, node_num]) 1st-inlink, 2nd-outlink
           (4) node_feats - list of [batch_size, node_num]
           (5) adjacent_matrix - 2 * [batch_size, real_node_num, real_neighbor_num] 1st-inlink, 2nd-outlink
    """
    def __init__(self, n_edge_type, d_model, d_rnn_enc_model, n_layer, alpha, d_feat_vec,
                 feat_vocab, edge_bias, layer_attn, dropout, attn_dropout):
        self.name = 'graph'
        super(SparseGraphEncoder, self).__init__()
        self.layer_attn = layer_attn

        self.hidden_size = d_model
        self.d_model = d_model
        ###=== node features ===###
        self.feature = True if feat_vocab else False
        if self.feature:
            self.feat_embs = nn.ModuleList([
                nn.Embedding(n_f_vocab, d_feat_vec, padding_idx=Constants.PAD) for n_f_vocab in feat_vocab
            ])
            self.hidden_size += d_feat_vec * len(feat_vocab)  
        ###=== edge embedding ===###
        self.edge_in_emb = nn.Embedding(n_edge_type, self.hidden_size * d_model, padding_idx=Constants.PAD)
        self.edge_out_emb = nn.Embedding(n_edge_type, self.hidden_size * d_model, padding_idx=Constants.PAD)
        self.edge_bias = edge_bias
        if edge_bias:
            self.edge_in_emb_bias = nn.Embedding(n_edge_type, d_model, padding_idx=Constants.PAD)
            self.edge_out_emb_bias = nn.Embedding(n_edge_type, d_model, padding_idx=Constants.PAD)
        ###=== graph encode layers===###
        self.layer_stack = nn.ModuleList([
            SparseGraphEncoderLayer(self.hidden_size, d_model, alpha, edge_bias=edge_bias, feature=self.feature,
                                    dropout=dropout, attn_dropout=attn_dropout) for _ in range(n_layer)
        ])
        ###=== gated output ===###
        self.gate = nn.Linear(d_model * 2, d_model, bias=False)

    @classmethod
    def from_opt(cls, opt):
        return cls(opt['n_edge_type'], opt['d_model'], opt['d_rnn_enc_model'], opt['n_layer'], opt['alpha'], 
                   opt['d_feat_vec'], opt['feat_vocab'], opt['edge_bias'], opt['layer_attn'], 
                   opt['dropout'], opt['attn_dropout'])
    
    def gated_output(self, outputs, inputs):
        concatenation = torch.cat((outputs, inputs), dim=2)
        g_t = torch.sigmoid(self.gate(concatenation))

        output = g_t * outputs + (1 - g_t) * inputs
        return output

    def forward(self, inputs):
        nodes, edges, mask = inputs['nodes'], inputs['edges'], inputs['mask']
        #node_feats, adjacent_matrix = inputs['feat_seqs'], inputs['adjacent_matrix']
        nodes = self.activate(nodes)
        node_output = nodes    # batch_size x node_num x d_model
        ###=== get embeddings ===###
        feat_hidden = None
        if self.feature:
            feat_hidden = [feat_emb(node_feat) for node_feat, feat_emb in zip(node_feats, self.feat_embs)]
            feat_hidden = torch.cat(feat_hidden, dim=2)     # batch_size x node_num x (hidden_size - d_model)
        # batch_size x (node_num * node_num) x hidden_size x d_model
        edge_in_hidden = self.edge_in_emb(edges).view(-1, self.hidden_size, nodes.size(2))
        edge_out_hidden = self.edge_out_emb(edges).view(-1, self.hidden_size, nodes.size(2))
        edge_hidden = (edge_in_hidden, edge_out_hidden)
        if self.edge_bias:
            # batch_size x (node_num * node_num) x d_model
            edge_in_hidden_bias, edge_out_hidden_bias = self.edge_in_emb_bias(edges), self.edge_out_emb_bias(edges)
        edge_hidden_bias = (edge_in_hidden_bias, edge_out_hidden_bias) if self.edge_bias else None
        ###=== forward ===###
        node_outputs = []
        for enc_layer in self.layer_stack:
            node_output = enc_layer(node_output, edge_hidden, mask, adjacent_matrix,
                                    feat_hidden=feat_hidden, edge_hidden_bias=edge_hidden_bias)
            node_outputs.append(node_output)
        
        node_output = self.gated_output(node_output, nodes)
        node_outputs[-1] = node_output

        hidden = [layer_output.transpose(0, 1)[0] for layer_output in node_outputs]
        
        if self.layer_attn:
            node_output = node_outputs
        
        return node_output, hidden


class EncoderTransformer(nn.Module):
    """Transform RNN-Encoder's output to Graph-Encoder's input
    Input: seq_output - [batch_size, seq_length, rnn_enc_dim] (tensor)
           root_list - [batch_size, node_num] (list)
           indexes_list - [batch_size, cross_num, node_num, word_num] (list)
           node_sizes - [batch_size * node_num, 1] (list)
    """
    def __init__(self, d_model, n_vocab, d_word_vec, d_k=64, device=None):
        super(EncoderTransformer, self).__init__()
        self.device = device
        self.d_k = d_k
        self.d_word_vec = d_word_vec
        self.word_emb = nn.Embedding(n_vocab, d_word_vec, padding_idx=Constants.PAD)
        self.attn = ConcatAttention(d_model, d_model, d_k)

    def forward(self, inputs, max_length):
        def pad(vectors, data_length, max_length=None):
            hidden_size = (max_length, vectors.size(1))
            out = torch.zeros(hidden_size, device=self.device)
            out.narrow(0, 0, data_length).copy_(vectors)    # bag_size x rnn_enc_dim
            return out
        seq_output, hidden, graph = inputs['seq_output'], inputs['hidden'], inputs['index']
        hidden = inputs['con_hidden']

        if isinstance(hidden, tuple) or isinstance(hidden, list) or hidden.dim() == 3:
            hidden = [h for h in hidden]
            hidden = torch.cat(hidden, dim=1)
        hidden = hidden.contiguous().view(hidden.size(0), -1)

        # root_list = inputs['root']
        #cross, nodes, words
        cross_lengths, node_sizes, node_lengths = inputs['cross_lengths'], inputs['lengths'], inputs['node_lengths']
        max_length = max(node_lengths)
        ##===== prepare vectors (do padding) =====##
        
        cross_roots, cross_bags, cross, cnt, sample_idx  = [], [], [], 0, 0
        # for sample_idx, sample in enumerate(zip(root_list, indexes_list)):
        #for indexes_list in graph:
        roots, bags = [], []
        for cross_idx, cross in enumerate(graph):
            for indexes in range(cross_lengths[cross_idx]):
                # root, indexes = sample[0], sample[1]
                # for root_idx, indexes_idx in zip(root, indexes):
                for indexes_idx in range(node_sizes[sample_idx]):
                    # roots.append(seq_output[sample_idx][root_idx])
                    roots.append(hidden[cross_idx])
                    # 用于给graph node初始化每个节点的权重
                    #bag = pad(word,node_lengths[cnt], max_length)
                    bag = pad(torch.stack([self.word_emb(cross[indexes][indexes_idx][idx]) for idx in range(node_lengths[cnt])], dim=0),
                          node_lengths[cnt], max_length)#TODO FIX MAX_LENGTH
                    #bag = pad(torch.stack([self.word_emb(idx) for idx in indexes_idx], dim=0),
                            #node_lengths[cnt], max_length)
                    bags.append(bag)
                    cnt += 1
                sample_idx += 1
            #max_cross_node = len(roots) if len(roots) > max_cross_node else max_cross_node
            #roots = torch.stack(roots, dim=0)   # all_node_num x rnn_enc_dim
            #bags = torch.stack(bags, dim=0)     # all_node_num x bag_size x rnn_enc_dim
            #cross_roots.append(roots)
            #cross_bags.append(bags)
        roots = torch.stack(roots, dim=0)   # all_node_num x rnn_enc_dim
        bags = torch.stack(bags, dim=0)     # all_node_num x bag_size x rnn_enc_dim
        '''
        for sample_idx, indexes in enumerate(indexes_list):
            # root, indexes = sample[0], sample[1]
            # for root_idx, indexes_idx in zip(root, indexes):
            for indexes_idx in indexes:
                # roots.append(seq_output[sample_idx][root_idx])
                roots.append(hidden[sample_idx])
                bag = pad(torch.stack([seq_output[sample_idx][idx] for idx in indexes_idx], dim=0), 
                          node_sizes[cnt], max_length)
                bags.append(bag)
                cnt += 1
        '''
        '''
        cross_roots_out = cross_roots[0].new_full((len(cross_roots), max_cross_node, self.d_word_vec), Constants.PAD)
        for i, root in enumerate(cross_roots):
            cross_roots_out[i].narrow(0, 0, len(root)).copy_(root)
        cross_bags_out = cross_bags[0].new_full((len(cross_bags), max_cross_node, max(node_lengths), self.d_word_vec), Constants.PAD)
        for i, bag in enumerate(cross_bags):
            cross_bags_out[i].narrow(0, 0, len(bag)).copy_(bag)
        '''
        #cross_roots = torch.stack(roots, dim=0)   # all_node_num x rnn_enc_dim
        #cross_bags = torch.stack(bags, dim=0)     # all_node_num x bag_size x rnn_enc_dim
        ##===== cross attention =====##
        context, *_ = self.attn(roots, bags)    # all_node_num x rnn_enc_dim
        ##===== get node vectors =====##
        max_length = max(node_sizes)
        max_cross = max(cross_lengths)
        node_len = 0
        cross_nodes = []
        for cross_len in cross_lengths:#每个ｃｒｏｓｓ含有的节点数
            nodes = []
            for c_l in range(cross_len):
                add_length = node_sizes[node_len]
                nodes.append(pad(context[:add_length], add_length, max_length))
                context = context[add_length:]
                node_len += 1
            nodes = torch.stack(nodes, dim=0)   # batch_size x node_num x d_model
            cross_nodes.append(nodes)

        cross_bags_out = cross_nodes[0].new_full((len(cross_nodes), max_cross, max_length, self.d_word_vec), Constants.PAD)
        for i, bag in enumerate(cross_nodes):
            cross_bags_out[i].narrow(0, 0, len(bag)).copy_(bag)

        return cross_bags_out, hidden

class EncoderCrossTransformer(nn.Module):
    """Transform RNN-Encoder's output to Graph-Encoder's input
    Input: seq_output - [batch_size, seq_length, rnn_enc_dim] (tensor)
           root_list - [batch_size, node_num] (list)
           indexes_list - [batch_size, cross_num, node_num, word_num] (list)
           node_sizes - [batch_size * node_num, 1] (list)
    """
    def __init__(self, d_model, n_vocab, d_word_vec, d_k=64, device=None):
        super(EncoderCrossTransformer, self).__init__()
        self.device = device
        self.d_k = d_k
        self.d_word_vec = d_word_vec
        self.word_emb = nn.Embedding(n_vocab, d_word_vec, padding_idx=Constants.PAD)
        self.attn = ConcatAttention(d_model, d_model, d_k)

    def forward(self, inputs, max_length):
        def pad(vectors, data_length, max_length=None):
            hidden_size = (max_length, vectors.size(1))
            out = torch.zeros(hidden_size, device=self.device)
            out.narrow(0, 0, data_length).copy_(vectors)    # bag_size x rnn_enc_dim
            return out
        def cross_pad(vectors, zero_vector):
            b_out = []
            for i, bag in enumerate(vectors):
                zero_vector.narrow(0, 0, len(bag)).copy_(bag)
                b_out.append(zero_vector)
            return b_out
    
        seq_output, hidden, graph = inputs['seq_output'], inputs['hidden'], inputs['index']
        hidden = inputs['con_hidden']

        if isinstance(hidden, tuple) or isinstance(hidden, list) or hidden.dim() == 3:
            hidden = [h for h in hidden]
            hidden = torch.cat(hidden, dim=1)
        hidden = hidden.contiguous().view(hidden.size(0), -1)

        # root_list = inputs['root']
        #cross, nodes, words
        cross_lengths, node_sizes, node_lengths = inputs['cross_lengths'], inputs['lengths'], inputs['node_lengths']
        max_length = max(node_lengths)
        max_cross = max(cross_lengths)
        max_nodesize = max(node_sizes)
        ##===== prepare vectors (do padding) =====##
        cross_roots, cross_bags, cross, cnt, sample_idx  = [], [], [], 0, 0
        # for sample_idx, sample in enumerate(zip(root_list, indexes_list)):
        #for indexes_list in graph:
        max_cross_node = 0
        for cross_idx, cross in enumerate(graph):
            #roots, bags = [], []
            cross_r, cross_b = [], []
            for indexes in range(cross_lengths[cross_idx]):
                roots, bags= [], []
                # root, indexes = sample[0], sample[1]
                # for root_idx, indexes_idx in zip(root, indexes):
                for indexes_idx in range(node_sizes[sample_idx]):
                    # roots.append(seq_output[sample_idx][root_idx])
                    roots.append(hidden[cross_idx])
                    # 用于给graph node初始化每个节点的权重
                    #bag = pad(word,node_lengths[cnt], max_length)
                    bag = pad(torch.stack([self.word_emb(cross[indexes][indexes_idx][idx]) for idx in range(node_lengths[cnt])], dim=0),
                          node_lengths[cnt], max_length)#TODO FIX MAX_LENGTH
                    #bag = pad(torch.stack([self.word_emb(idx) for idx in indexes_idx], dim=0),
                            #node_lengths[cnt], max_length)
                    bags.append(bag)
                    cnt += 1
                sample_idx += 1
                cross_r.append(torch.stack(roots, dim=0))
                cross_b.append(torch.stack(bags, dim=0))
            #cross_r = torch.stack(cross_r, dim=0) # all_node_num x rnn_enc_dim
            #cross_b = torch.stack(cross_b, dim=0) # all_node_num x bag_size x rnn_enc_dim  
            
            cross_roots_out = cross_r[0].new_full((max_nodesize, self.d_word_vec), Constants.PAD)
            r_out = cross_pad(cross_r, cross_roots_out)
            cross_roots.append(torch.stack(r_out,dim = 0))
            '''
            r_out = []
            for i, root in enumerate(cross_r):
                cross_roots_out.narrow(0, 0, len(root)).copy_(root)
                r_out.append(cross_roots_out)
            cross_roots.append(torch.stack(r_out,dim = 0))
            '''

            cross_bags_out = cross_b[0].new_full((max_nodesize, max_length, self.d_word_vec), Constants.PAD)
            b_out = cross_pad(cross_b, cross_bags_out)
            cross_bags.append(torch.stack(b_out,dim = 0))
            
            '''
            b_out = []
            for i, bag in enumerate(cross_b):
                cross_bag_out.narrow(0, 0, len(bag)).copy_(bag)
                b_out.append(cross_bag_out)
            cross_bags.append(torch.stack(b_out,dim = 0))
            '''
        '''
        for sample_idx, indexes in enumerate(indexes_list):
            # root, indexes = sample[0], sample[1]
            # for root_idx, indexes_idx in zip(root, indexes):
            for indexes_idx in indexes:
                # roots.append(seq_output[sample_idx][root_idx])
                roots.append(hidden[sample_idx])
                bag = pad(torch.stack([seq_output[sample_idx][idx] for idx in indexes_idx], dim=0), 
                          node_sizes[cnt], max_length)
                bags.append(bag)
                cnt += 1
        '''
        '''
        cross_roots_out = cross_roots[0][0].new_full((len(cross_roots), max_cross, self.d_word_vec), Constants.PAD)
        for i, root in enumerate(cross_roots):
            cross_roots_out[i].narrow(0, 0, len(root)).copy_(root)
        cross_bags_out = cross_bags[0].new_full((len(cross_bags), max_cross, max(node_lengths), self.d_word_vec), Constants.PAD)
        for i, bag in enumerate(cross_bags):
            cross_bags_out[i].narrow(0, 0, len(bag)).copy_(bag)
        '''
        #cross_roots = torch.stack(roots, dim=0)   # all_node_num x rnn_enc_dim
        #cross_bags = torch.stack(bags, dim=0)     # all_node_num x bag_size x rnn_enc_dim
        ##===== cross attention =====##
        batch_node = []
        for bat_c, bat_r in zip(cross_bags, cross_roots):
            cross_nodes = []
            cross_score = []
            for cro_c, cro_t in zip(bat_r, bat_c):
                context, *_ = self.attn(cro_c, cro_t)    # all_node_num x rnn_enc_dim
                cross_score.append(torch.sum(context))
                cross_nodes.append(context)
            max_score_index = cross_score.index(max(cross_score))
            while(max_score_index):
                cross_nodes[max_score_index-1].fill_(0)
                max_score_index -= 1
            batch_node.append(torch.stack(cross_nodes, dim = 0))
        ##===== get node vectors =====##
        
        node_len = 0
        '''
        cross_nodes = []
        for cross_len in cross_lengths:#每个ｃｒｏｓｓ含有的节点数
            nodes = []
            for c_l in range(cross_len):
                add_length = node_sizes[node_len]
                nodes.append(pad(contextes[:add_length], add_length, max_length))
                contextes = contextes[add_length:]
                node_len += 1
            nodes = torch.stack(nodes, dim=0)   # batch_size x node_num x d_model
            cross_nodes.append(nodes)
        ''' 
        
        cross_bags_out = batch_node[0].new_full((len(batch_node), max_cross, max_nodesize, self.d_word_vec), Constants.PAD)
        for i, bag in enumerate(batch_node):
            cross_bags_out[i].narrow(0, 0, len(bag)).copy_(bag)

        return cross_bags_out, hidden


class TransfEncoder(nn.Module):
    """
    Input: (1) inputs['src_seq']
           (2) inputs['src_pos']
           (3) inputs['feat_seqs']
    Output: (1) enc_output
            (2) hidden
    """
    def __init__(self, n_vocab, pretrained=None, model_name='default', layer_attn=False):
        self.name = 'transf'
        self.model_type = model_name

        super(TransfEncoder, self).__init__()

        self.layer_attn = layer_attn
        self.pretrained = pretrained

        if model_name == 'bert':
            self.d_enc_model = 768
            self.d_head = 8
            self.n_enc_layer = 12
    
    @classmethod
    def from_opt(cls, opt):
        if opt['pretrained'].count('bert'):
            pretrained = BertModel.from_pretrained(opt['pretrained'])
            return cls(opt['n_vocab'], pretrained=pretrained, layer_attn=opt['layer_attn'], model_name='bert')
        else:
            raise ValueError("Other pretrained models haven't been supported yet")

    def forward(self, inputs, return_attns=False):
        src_seq = inputs['src_seq']
        
        if self.model_type == 'bert':
            enc_outputs, *_ = self.pretrained(src_seq, output_all_encoded_layers=True)
            enc_output = enc_outputs[-1]
        
        hidden = [layer_output.transpose(0, 1)[0] for layer_output in enc_outputs]
        if self.layer_attn:
            enc_output = enc_outputs
            
        return enc_output, hidden