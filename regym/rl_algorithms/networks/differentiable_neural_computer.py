from typing import Dict, List, Optional, Callable

import torch
import torch.nn as nn
import torch.nn.functional as F

from .utils import layer_init, layer_init_lstm, layer_init_gru

from regym.rl_algorithms.utils import _extract_from_rnn_states, extract_subtree, copy_hdict
from regym.rl_algorithms.networks.bodies import LSTMBody

import wandb

def _register_nan_checks(model):
    def check_grad(module, grad_in, grad_out):
        #wandb.log({f"{type(module).__name__}_gradients": wandb.Histogram(grad_in)})
        if any([torch.any(torch.isnan(gi.data)) for gi in grad_in if gi is not None]):
            print(type(module).__name__)
            import ipdb; ipdb.set_trace()

    model.apply(lambda module: module.register_backward_hook(check_grad))


class BasicDNCHeads(nn.Module):
    def __init__(
        self,
        memory, 
        input_dim=256, 
        nbr_heads=1, 
        ):
        super(BasicDNCHeads,self).__init__()

        self.memory = memory
        self.mem_dim = self.memory.mem_dim
        self.nbr_heads = nbr_heads
        self.input_dim = input_dim

        self.generate_ctrl2gate()

    def generate_ctrl2gate(self) :
        # Generates:
        # kr: read keys
        self.head_gate_dim = self.nbr_heads*self.memory.mem_dim
        # read strenghs betar:
        self.head_gate_dim += self.nbr_heads*1
        
        # free gates f:
        self.head_gate_dim += self.nbr_heads*1 
        # read modes pi:
        self.head_gate_dim += self.nbr_heads*3
        
        # kw write keys:
        self.head_gate_dim += self.memory.mem_dim
        # write strengths betaw:
        self.head_gate_dim += 1
            
        # erase vector e:
        self.head_gate_dim += self.memory.mem_dim
        # wrte vector v:
        self.head_gate_dim += self.memory.mem_dim
        # allocation gate ga:
        self.head_gate_dim += 1
        # write gate gw:
        self.head_gate_dim += 1
        
        self.ctrl2head = layer_init(
            nn.Linear(
                self.input_dim, 
                self.head_gate_dim
            ),
            w_scale=1e-3,
            init_type='ortho',
        )
    
    def write(self, memory_state, ctrl_inputs):
        raise NotImplementedError

    def read(self, memory_state, ctrl_inputs):
        raise NotImplementedError

    def forward(self, memory_state, ctrl_inputs):
        # WARNING: it is imperative to make a copy 
        # of the frame_state, otherwise any changes 
        # will be repercuted onto the current frame_state
        x = ctrl_inputs
      
        ctrl_output = self.ctrl2head(x)
        #ctrl_output = ctrl_output.view((-1, self.nbr_heads, self.head_gate_dim))

        odict = self._generate_addressing(ctrl_output)

        return odict
           
    def _generate_addressing(self, ctrl_output) :
        odict = {}
        
        start = 0
        end = self.nbr_heads*self.mem_dim
        #odict['kr'] = ctrl_output[:,start:end].reshape(-1, self.nbr_heads, self.mem_dim)
        odict['kr'] = torch.tanh(ctrl_output[:,start:end]).reshape(-1, self.nbr_heads, self.mem_dim)
        start = end
        end += self.nbr_heads
        odict['betar'] = F.softplus(ctrl_output[:,start:end]).reshape(-1, self.nbr_heads, 1)
        # no need for 1+ :  https://github.com/deepmind/dnc/issues/9
        start = end
        end += self.mem_dim
        #odict['kw'] = ctrl_output[:,start:end].reshape(-1, 1, self.mem_dim)
        odict['kw'] = torch.tanh(ctrl_output[:,start:end]).reshape(-1, 1, self.mem_dim)
        start = end
        end += 1
        odict['betaw'] = F.softplus(ctrl_output[:,start:end]).reshape(-1, 1, 1)
        # no need for 1+ :  https://github.com/deepmind/dnc/issues/9

        start = end
        end += self.mem_dim
        # (batch_size, 1, mem_dim)
        odict['erase'] = torch.sigmoid(ctrl_output[:,start:end]).reshape(-1, 1, self.mem_dim)
        
        start = end
        end += self.mem_dim
        # (batch_size, 1, mem_dim)
        #odict['write'] = ctrl_output[:,start:end].reshape(-1, 1, self.mem_dim)
        odict['write'] = torch.tanh(ctrl_output[:,start:end]).reshape(-1, 1, self.mem_dim)
        
        start = end 
        end += 1
        odict['ga'] = torch.sigmoid(ctrl_output[:,start:end]).reshape(-1, 1, 1)
        start = end
        end += 1
        odict['gw'] = torch.sigmoid(ctrl_output[:,start:end]).reshape(-1, 1, 1)
        
        start = end 
        end += self.nbr_heads
        odict['f'] = torch.sigmoid(ctrl_output[:,start:end]).reshape(-1, self.nbr_heads, 1)
        
        start = end 
        end += 3*self.nbr_heads
        odict['pi'] = torch.softmax(
            ctrl_output[:,start:end].reshape(-1, self.nbr_heads, 3),
            dim=-1,
        )

        return odict

    
class ReadWriteHeads(BasicDNCHeads):
    def __init__(
        self, 
        memory, 
        nbr_heads=1, 
        input_dim=256,
        ):
        super(ReadWriteHeads,self).__init__(
            memory=memory,
            input_dim=input_dim,
            nbr_heads=nbr_heads,
        )
    
    def _update_usage_vector(
        self,
        prev_usage_vector,
        free_gates,
        prev_read_weights,
        prev_write_weights,
        ):
        batch_size = prev_usage_vector.shape[0]
        # ensure minimum usage for stability:
        prev_usage_vector = 5e-3+(1-5e-3)*prev_usage_vector
        
        # write_weights = write_weights.detach()  # detach from the computation graph
        # (batch_size x nbr_read/write_heads x mem_nbr_slots)
        psi = torch.prod(1 - free_gates.reshape(batch_size, -1, 1) * prev_read_weights, dim=1)
        # (batch_size x nbr_mem_slots)
        #wandblog({f"psi": wandb.Histogram(psi.cpu().detach())})
        
        # if we only had one write head:
        # usage = prev_usage_vector + pev_write_weights -prev_usage_vector*prev_write_weights
        # with multiple write head:
        ## the more we write, the more usage increases:
        ## because these values are weights in [0,1],
        ## multiplying them together reduces the usage,
        ## unless we multiple together the opposite probabilities on each slots,
        ## thus reducing the overal opposite probabilities, and increasing 
        ## the probability of the event of using a given memory slot.
        ## Thus, we take againt the opposite probabilty of those successive events:
        reg_prev_write_weights = (1-torch.prod(1-prev_write_weights, dim=1))
        # (batch_size x mem_nbr_slots)
        usage = prev_usage_vector + (1 - prev_usage_vector) * reg_prev_write_weights
        usage = usage * psi
        return usage

    def forward(
        self,
        memory_state,
        ctrl_inputs,
        ):
        odict = super(ReadWriteHeads, self).forward(
            memory_state=memory_state,
            ctrl_inputs=ctrl_inputs,
        )
        return odict

    def write(
        self, 
        memory_state, 
        odict, 
        prev_usage_vector,
        prev_read_weights,
        prev_write_weights,
        ):
        batch_size = prev_usage_vector.shape[0]
        updated_usage_vector = self._update_usage_vector(
            prev_usage_vector=prev_usage_vector,
            free_gates=odict['f'],
            prev_read_weights=prev_read_weights,
            prev_write_weights=prev_write_weights,
        )
        # (batch_size x mem_nbr_slots)
        #wandb.log({f"usage": wandb.Histogram(updated_usage_vector.cpu().detach())})
        
        # Adapted from:
        # https://github.com/ixaxaar/pytorch-dnc/blob/33e35326db74c7ccd45360d6668682e60b407d1f/dnc/memory.py#L84
        ## Compute free list:
        sorted_usage, phi = torch.topk(
            updated_usage_vector,
            k=self.memory.mem_nbr_slots,
            dim=1,
            largest=False,
        )

        ## Compute 1-index-delayed cum. product of sorted usages:
        delayed_sorted_usage = torch.cat([
            torch.ones(*sorted_usage.shape[:-1], 1).to(phi.device),
            sorted_usage,],
            dim=-1,
        )
        delayed_prod_sorted_usage = torch.cumprod(
            delayed_sorted_usage,
            dim=-1,
        )[...,:-1] # j-th slot only gets the cumprod till (j-1)-th slot.
        
        sorted_allocation_weights = (1-sorted_usage)*delayed_prod_sorted_usage
        #(batch_size x mem_nbr_slots)
        
        # Unsort allocation weights 
        # by reversing sorting (== by sorting the sorted indices):
        _, unsorted_indices = torch.topk(
            phi,
            k=self.memory.mem_nbr_slots,
            dim=1,
            largest=False,
        )
        # and then re-order the sorted allocation weights:
        allocation_weights = torch.gather(
            sorted_allocation_weights,
            dim=1,
            index=unsorted_indices.long(),
        ).reshape(batch_size, 1, self.memory.mem_nbr_slots)
        # (batch_size x 1 x mem_nbr_slots)
        #wandb.log({f"allocation": wandb.Histogram(allocation_weights.cpu().detach())})

        # Content Addressing :
        wc = self.memory.content_addressing(memory_state, odict['kw'], odict['betaw'])
        #wandblog({f"write_content": wandb.Histogram(wc.cpu().detach())})

        # Interpolation between content and allocation:
        write_weights = odict['gw']*(odict['ga']*allocation_weights+(1-odict['ga'])*wc)
        #(batch_size x 1 x nbr_mem_slots  )
        new_memory_state = self.memory.write(
            memory_state=memory_state,
            w=write_weights,
            erase=odict['erase'],
            add=odict['write'],
        )
        
        odict['usage_vector'] = updated_usage_vector
        odict['write_weights'] = write_weights
        odict['allocation_weights'] = allocation_weights

        return new_memory_state, updated_usage_vector, write_weights 
    
    def read(
        self, 
        memory_state, 
        odict,
        write_weights,
        prev_link_matrix,
        prev_precedence_weights,
        prev_read_weights,
        ):
        batch_size = write_weights.shape[0]
        # update temporal link matrix:
        # Adapted from:
        #https://github.com/ixaxaar/pytorch-dnc/blob/33e35326db74c7ccd45360d6668682e60b407d1f/dnc/memory.py#L111
        wi = write_weights.reshape(-1, self.memory.mem_nbr_slots, 1)
        wj = write_weights.reshape(-1, 1, self.memory.mem_nbr_slots)
        scaler = (1-wi-wj)

        prev_pj = prev_precedence_weights.reshape(-1, 1, self.memory.mem_nbr_slots)
        add = wi*prev_pj

        updated_link_matrix = scaler*prev_link_matrix + add
        # (batch_size, mem_nbr_slots, mem_nbr_slots)

        # regularize diagonal:
        """
        for i in range(self.memory.mem_nbr_slots):
            updated_link_matrix[:, i,i] = 0
        """
        eye = 1-torch.eye(self.memory.mem_nbr_slots).unsqueeze(0).to(wi.device)
        updated_link_matrix = eye.expand_as(updated_link_matrix)*updated_link_matrix

        odict['link_matrix'] = updated_link_matrix
        #wandblog({f"link_matrix": wandb.Histogram(updated_link_matrix.cpu().detach())})

        # update precedence weights:
        sum_w = write_weights.reshape(-1, self.memory.mem_nbr_slots).sum(dim=-1).reshape(batch_size, 1, 1)
        # (batch_size, 1, 1)
        updated_precedence_weights = (1-sum_w)*prev_precedence_weights+write_weights
        #(batch_size, 1, mem_nbr_slots)
        
        odict['precedence_weights'] = updated_precedence_weights 
        #wandb.log({f"precedence_weights": wandb.Histogram(updated_precedence_weights.cpu().detach())})
                
        # forward weighting:
        ## allow broadcasting over head dimension:
        blm = updated_link_matrix.unsqueeze(1)
        # (batch_size, 1, nbr_mem_slots, nbr_mem_slots)
        prw = prev_read_weights.reshape(batch_size, 1, self.nbr_heads, -1).transpose(2, 3)
        # (batch_size, 1, nbr_mem_slots, nbrHeads)
        forward_weights = torch.matmul(blm, prw).squeeze(1).transpose(1, 2)
        # (batch_size, nbrHeads, nbr_mem_slots)
        # backward weighting:
        backward_weights = torch.matmul(blm.transpose(2, 3), prw).squeeze(1).transpose(1, 2)
        #( batch_size, nbrHeads, nbr_mem_slots)

        # Content Addressing :
        content_weights = self.memory.content_addressing(memory_state, odict['kr'], odict['betar'])
        
        # Interpolation over reading modes:
        read_modes_scaler = odict['pi'].reshape(-1, self.nbr_heads, 1, 3)
        read_weights_mult = torch.cat([
            backward_weights.reshape(-1, self.nbr_heads, self.memory.mem_nbr_slots, 1),
            content_weights.reshape(-1, self.nbr_heads, self.memory.mem_nbr_slots, 1),
            forward_weights.reshape(-1, self.nbr_heads, self.memory.mem_nbr_slots, 1),
            ],
            dim=-1,
        )
        # (batch_size, nbr_heads, nbr_mem_slots, 3)
        read_weights = read_modes_scaler.expand_as(read_weights_mult)*read_weights_mult
        read_weights = read_weights.sum(dim=-1, keepdim=False)
        
        odict['read_weights'] = read_weights
        #wandblog({f"forward_weights": wandb.Histogram(forward_weights.cpu().detach())})
        #wandblog({f"backward_weights": wandb.Histogram(backward_weights.cpu().detach())})
        #wandblog({f"content_weights": wandb.Histogram(content_weights.cpu().detach())})
        #wandb.log({f"read_modes_scaler": wandb.Histogram(read_modes_scaler.cpu().detach())})

        read_vectors = self.memory.read(memory_state=memory_state, w=read_weights)
        odict['read_vectors'] = read_vectors
        #wandblog({f"read_vectors": wandb.Histogram(read_vectors.cpu().detach())})

        return read_vectors, read_weights, updated_precedence_weights, updated_link_matrix


class DNCController(LSTMBody):
    def __init__(
        self, 
        input_dim=32, 
        hidden_units=[512], 
        output_dim=32, 
        mem_nbr_slots=128, 
        mem_dim= 32, 
        nbr_read_heads=1, 
        nbr_write_heads=1,
        extra_inputs_infos: Optional[Dict]={},
        ):
        """
        :param extra_inputs_infos: Dictionnary containing the shape of the lstm-relevant extra inputs.
        """

        #LSTMinput_size = (input_dim+output_dim)+mem_dim*nbr_read_heads
        LSTMinput_size = input_dim
        # output_dim was added in the context of few-shot learning 
        # where the previous desired output is fed as input alongside the new input.
        # mem_dim*nbr_read_heads are implicit parts that must be taken into account:
        # they are out-of-concern here, though:
        # the NTM module is itself adding them to the input...
        
        super(DNCController, self).__init__(
            state_dim=LSTMinput_size,
            hidden_units=hidden_units,
            gate=None,
            extra_inputs_infos=extra_inputs_infos,
        )

        self.input_dim = input_dim
        self.hidden_units = hidden_units
        self.output_dim = output_dim
        self.mem_nbr_slots = mem_nbr_slots
        self.mem_dim = mem_dim
        self.nbr_read_heads = nbr_read_heads
        self.nbr_write_heads = nbr_write_heads

        self.build_controller()

    def build_controller(self):
        controller_lstm_output_dim = self.hidden_units[-1]
        # Output Function:
        self.linear_output = layer_init(
            nn.Linear(
                controller_lstm_output_dim,
                self.output_dim,
            ),
            w_scale=1e-3,
            init_type='ortho',
        )
        
        # External Outputs :
        self.output_fn = []
        # input = (r0_{t}, ..., rN_{t})
        self.EXTinput_size = self.mem_dim * self.nbr_read_heads
        self.output_fn.append( 
            layer_init(
                nn.Linear(
                    self.EXTinput_size, 
                    self.output_dim
                ),
                w_scale=1e-3,
            )
        )
        
        self.output_fn = nn.Sequential(*self.output_fn)

    def forward_external_output_fn(self, vt_output, slots_read) :
        batch_size = slots_read.shape[0]
        rslots_read = slots_read.reshape(batch_size, -1)
        output_fn_output = vt_output + self.output_fn(rslots_read)
        
        return output_fn_output
    
    def forward_controller(self, inputs):
        '''
        :param inputs: input to LSTM cells. Structured as (feed_forward_input, {hidden: hidden_states, cell: cell_states}).
        hidden_states: list of hidden_state(s) one for each self.layers.
        cell_states: list of hidden_state(s) one for each self.layers.
        '''
        # WARNING: it is imperative to make a copy 
        # of the frame_state, otherwise any changes 
        # will be repercuted onto the current frame_state
        x, frame_states = inputs[0], copy_hdict(inputs[1])
        
        recurrent_neurons = extract_subtree(
            in_dict=frame_states,
            node_id='lstm',
        )

        extra_inputs = extract_subtree(
            in_dict=frame_states,
            node_id='extra_inputs',
        )

        extra_inputs = [v[0].to(x.dtype).to(x.device) for v in extra_inputs.values()]
        if len(extra_inputs): x = torch.cat([x]+extra_inputs, dim=-1)

        if next(self.layers[0].parameters()).is_cuda and not(x.is_cuda):    x = x.cuda() 
        hidden_states, cell_states = recurrent_neurons['hidden'], recurrent_neurons['cell']

        next_hstates, next_cstates = [], []
        outputs = []
        for idx, (layer, hx, cx) in enumerate(zip(self.layers, hidden_states, cell_states) ):
            batch_size = x.size(0)
            if hx.size(0) == 1: # then we have just resetted the values, we need to expand those:
                hx = torch.cat([hx]*batch_size, dim=0)
                cx = torch.cat([cx]*batch_size, dim=0)
            elif hx.size(0) != batch_size:
                raise NotImplementedError("Sizes of the hidden states and the inputs do not coincide.")

            if next(layer.parameters()).is_cuda and \
                (hx is not None or not(hx.is_cuda)) and \
                (cx is  not None or not(cx.is_cuda)):
                if hx is not None:  hx = hx.cuda()
                if cx is not None:  cx = cx.cuda() 

            """
            nhx, ncx = layer(x, (hx, cx))
            next_hstates.append(nhx)
            next_cstates.append(ncx)
            """
            # VDN:
            if len(x.shape)==3:
                raise NotImplementedError
                shapex = x.shape
                shapehx = hx.shape
                shapecx = cx.shape 
                x = x.reshape(-1, shapex[-1])
                hx = hx.reshape(-1, shapehx[-1])
                cx = cx.reshape(-1, shapecx[-1])
                nhx, ncx = layer(x, (hx, cx))
                nhx = nhx.reshape(*shapehx[:2], -1)
                ncx = ncx.reshape(*shapecx[:2], -1)
            else:
                nhx, ncx = layer(x, (hx, cx))

            outputs.append([nhx, ncx])
            next_hstates.append(outputs[-1][0])
            next_cstates.append(outputs[-1][1])
            
            # Consider not applying activation functions on last layer's output?
            if self.gate is not None:
                x = self.gate(outputs[-1][0])
            else:
                x = outputs[-1][0]
        
        vt = self.linear_output(x.reshape(batch_size,-1))

        frame_states.update({'lstm':
            {'hidden': next_hstates, 
            'cell': next_cstates}
        })

        return vt, x, frame_states
    
    def get_reset_states(self, cuda=False, repeat=1):
        hidden_states, cell_states = [], []
        for layer in self.layers:
            h = torch.zeros(repeat, layer.hidden_size)
            if cuda:
                h = h.cuda()
            hidden_states.append(h)
            cell_states.append(h)
        return {'lstm':{'hidden': hidden_states, 'cell': cell_states}}

    def get_feature_shape(self):
        return self.output_dim


class DNCMemory(nn.Module) :
    def __init__(
        self, 
        mem_nbr_slots, 
        mem_dim, 
        ):
        
        super(DNCMemory,self).__init__()

        self.mem_nbr_slots = mem_nbr_slots
        self.mem_dim = mem_dim
        
        self.initialize_memory()

    def initialize_memory(self) :
        # Constant 
        ## Null:
        self.init_mem = torch.zeros((1, self.mem_nbr_slots,self.mem_dim))
        ## Small:
        #self.init_mem = 1e-6*torch.ones((1, self.mem_nbr_slots,self.mem_dim))
        
    def get_reset_states(self, cuda=False, repeat=1):
        memory = []
        h = self.init_mem.clone().repeat(repeat, 1 , 1)
        if cuda:
            h = h.cuda()
        memory.append(h)
        return {'memory': memory}

    def content_addressing(
        self,
        memory,
        k,
        beta
        ):
        batch_size = k.shape[0]
        nbrHeads = k.size()[1]
        eps = 1e-10
        
        #memory_bhSMidx = torch.cat([memory.unsqueeze(1)]*nbrHeads, dim=1).to(k.device)
        memory_bhSMidx = memory.unsqueeze(1).repeat(1,nbrHeads,1,1).to(k.device)
        # (batch_size, nbrHeads, nbr_mem_slot, mem_dim)
        #kmat = torch.cat([k.unsqueeze(2)]*self.mem_nbr_slots, dim=2)
        kmat = k.unsqueeze(2)
        # (batch_size, nbrHeards, 1, nbr_mem_slot)
        cossim = F.cosine_similarity( kmat, memory_bhSMidx, dim=-1)
        #(batch_size x nbrHeads nbr_mem_slot )
        w = F.softmax( beta * cossim, dim=-1)
        #(batch_size x nbrHeads nbr_mem_slot )
        # beta : (batch_size x nbrHeads x 1)
        return w 

    def write(
        self, 
        memory_state, 
        w, 
        erase, 
        add,
        ):
        # erase/add: (batch_size, nbrHeads, mem_dim)
        # w: (batch_size, nbrHeads, nbr_mem_slot)
        # memory_state: (batch_size, nbr_mem_slot, mem_dim)
        batch_size = w.shape[0]
        nmemory = memory_state

        nh = erase.shape[1]
        e = torch.matmul(w.unsqueeze(-1), erase.unsqueeze(2))
        a = torch.matmul(w.unsqueeze(-1), add.unsqueeze(2))
        for hidx in range(nh):
            nmemory = nmemory*(1-e[:,hidx])+a[:,hidx]
        return nmemory

    def read(self, memory_state, w):
        reading = torch.matmul(w, memory_state)
        #(batch_size x nbrHeads x mem_dim)
        return reading
        

class DNCBody(nn.Module) :
    def __init__(
        self,
        input_dim=32, 
        hidden_units=512, 
        output_dim=32, 
        mem_nbr_slots=128, 
        mem_dim= 32, 
        nbr_read_heads=1, 
        nbr_write_heads=1, 
        clip=20,
        extra_inputs_infos: Optional[Dict]={},
        ):

        super(DNCBody,self).__init__()

        self.input_dim = input_dim
        self.hidden_units = hidden_units
        self.hidden_dim = hidden_units[-1]
        self.output_dim = output_dim
        self.extra_inputs_infos = extra_inputs_infos

        self.mem_nbr_slots = mem_nbr_slots
        self.mem_dim = mem_dim
        
        assert nbr_write_heads==1
        self.nbr_read_heads = nbr_read_heads
        self.nbr_write_heads = nbr_write_heads
        
        self.clip = clip 

        self.build_memory()
        self.build_controller()
        self.build_heads()
        
        #_register_nan_checks(self)

    def build_memory(self) :
        self.memory = DNCMemory(
            mem_nbr_slots=self.mem_nbr_slots,
            mem_dim=self.mem_dim,
        )
        
    def build_controller(self) :
        self.controller = DNCController( 
            # taking into account the previously read vec:
            input_dim=self.input_dim+self.mem_dim*self.nbr_read_heads, 
            hidden_units=self.hidden_units, 
            output_dim=self.output_dim, 
            mem_nbr_slots=self.mem_nbr_slots, 
            mem_dim=self.mem_dim, 
            nbr_read_heads=self.nbr_read_heads, 
            nbr_write_heads=self.nbr_write_heads,
            extra_inputs_infos=self.extra_inputs_infos,
        )

    def build_heads(self) :
        self.readWriteHeads = ReadWriteHeads(
            memory=self.memory,
            nbr_heads=self.nbr_read_heads, 
            input_dim=self.hidden_dim, 
        )
    
    """
    def init_prev_w(self):
        #attr_id = f"{'read' if self.is_read else 'write'}_prev_w"
        attr_id = "prev_w"
        setattr(self, attr_id, nn.Parameter(torch.zeros(1, self.nbr_heads, self.memory.mem_nbr_slots)))
    """

    def _reset_weights(self, cuda=False, repeat=1, nbr_heads=1):
        # Constant:
        prev_w = torch.zeros((repeat, nbr_heads, self.mem_nbr_slots))
        # Constant with diversity:
        """
        prev_w = []
        for hidx in range(nbr_heads):
            offset = nbr_heads
            hw = torch.zeros(repeat, 1, self.mem_nbr_slots)
            hw[...,hidx+offset] = 1.0
            prev_w.append(hw)
        prev_w = torch.cat(prev_w, dim=1)
        """
        # Learnable:
        # prev_w = self.prev_w.repeat(repeat, 1, 1) 
        if cuda:
            prev_w = prev_w.cuda()
        return [prev_w]
            
    def get_reset_states(self, cuda=False, repeat=1):
        prev_read_vec = []
        h = torch.zeros(repeat, self.nbr_read_heads*self.mem_dim)
        if cuda:
            h = h.cuda()
        prev_read_vec.append(h)

        prev_usage_vector = []
        h = torch.zeros(repeat, self.mem_nbr_slots)
        if cuda:    h = h.cuda()
        prev_usage_vector.append(h)

        prev_read_weights = self._reset_weights(
            cuda=cuda, 
            repeat=repeat,
            nbr_heads=self.nbr_read_heads,
        )
        
        prev_write_weights = []
        h = torch.zeros(repeat, self.nbr_write_heads, self.mem_nbr_slots)
        if cuda:    h = h.cuda()
        prev_write_weights.append(h)
       
        prev_link_matrix = []
        h = torch.zeros(repeat, self.mem_nbr_slots, self.mem_nbr_slots)
        if cuda:    h = h.cuda()
        prev_link_matrix.append(h)
        
        prev_precedence_weights = []
        h = torch.zeros(repeat, 1, self.mem_nbr_slots)
        if cuda:    h = h.cuda()
        prev_precedence_weights.append(h)
 
        ## As an encapsulating module, it is its responsability
        # to call get_reset_states on the encapsulated elements:
        hdict = {'dnc_body':
            {
                'prev_read_vec': prev_read_vec,
                'prev_usage_vector': prev_usage_vector,
                'prev_read_weights': prev_read_weights,
                'prev_write_weights': prev_write_weights,
                'prev_link_matrix': prev_link_matrix,
                'prev_precedence_weights': prev_precedence_weights,
            },
        }

        hdict['dnc_controller'] = self.controller.get_reset_states(repeat=repeat, cuda=cuda)
        hdict['dnc_memory'] = self.memory.get_reset_states(repeat=repeat, cuda=cuda)
        return {'dnc':hdict}

    def forward(self, inputs):
        # DNC_input :
        # 'input' : batch_dim x seq_len x self.input_dim
        # 'prev_desired_output' : batch_dim x seq_len x self.output_dim
        # 'prev_read_vec' : batch_dim x seq_len x self.nbr_read_head * self.mem_dim
        #x['prev_read_vec'] = self.read_outputs[-1]
        # Taking into account the previously read vector as a state:
        x, frame_states = inputs[0], copy_hdict(inputs[1])
        batch_size = x.shape[0]

        dnc_state_dict = extract_subtree(
            in_dict=frame_states,
            node_id='dnc',
        )

        prev_read_vec = dnc_state_dict['dnc_body']['prev_read_vec'][0]
        prev_read_vec = prev_read_vec.to(x.dtype).to(x.device)
        x = torch.cat([x, prev_read_vec], dim=-1)
        
        #wandblog({f"prev_read_vec": wandb.Histogram(prev_read_vec.cpu().detach())})

        # Controller Outputs :
        # output : batch_dim x hidden_dim
        # state : ( h, c) 
        controller_inputs = [x, dnc_state_dict['dnc_controller']]
        vt, nx, dnc_state_dict['dnc_controller'] = self.controller.forward_controller(controller_inputs)
        
        #wandb.log({f"vt": wandb.Histogram(vt.cpu().detach())})
        #wandblog({f"nx": wandb.Histogram(nx.cpu().detach())})
        
        # clip the controller output
        nx = torch.clamp(nx, -self.clip, self.clip)
        
        memory_state = dnc_state_dict['dnc_memory']['memory'][0].to(x.device) 
        
        #wandblog({f"memory": wandb.Histogram(memory_state.cpu().detach())})
        
        prev_read_weights = dnc_state_dict['dnc_body']['prev_read_weights'][0].to(vt.device)
        prev_write_weights = dnc_state_dict['dnc_body']['prev_write_weights'][0].to(vt.device)
        #(batch_size x nbrHeads x nbr_mem_slot )
        prev_usage_vector = dnc_state_dict['dnc_body']['prev_usage_vector'][0].to(vt.device)
        #(batch_size x nbrHeads x nbr_mem_slot )

        # Memory Write :
        odict = self.readWriteHeads(
            memory_state=memory_state,
            ctrl_inputs=nx,
        )
        written_memory_state, new_usage_vector, new_write_weights =self.readWriteHeads.write(
            memory_state=memory_state,
            odict=odict,
            prev_usage_vector=prev_usage_vector,
            prev_read_weights=prev_read_weights,
            prev_write_weights=prev_write_weights,
        )
        # updating frame state:
        dnc_state_dict['dnc_body']['prev_usage_vector'] = [new_usage_vector]
        dnc_state_dict['dnc_body']['prev_write_weights'] = [new_write_weights]
        
        prev_link_matrix = dnc_state_dict['dnc_body']['prev_link_matrix'][0].to(vt.device)
        prev_precedence_weights = dnc_state_dict['dnc_body']['prev_precedence_weights'][0].to(vt.device)
        # Memory Read :
        # batch_dim x nbr_read_heads * mem_dim :
        read_vec, new_read_weights, \
        updated_precedence_weights, updated_link_matrix = self.readWriteHeads.read(
            memory_state=memory_state,
            odict=odict,
            write_weights=new_write_weights,
            prev_link_matrix=prev_link_matrix,
            prev_precedence_weights=prev_precedence_weights,
            prev_read_weights=prev_read_weights,
        )

        # updating frame state:
        dnc_state_dict['dnc_body']['prev_link_matrix'] = [updated_link_matrix]
        dnc_state_dict['dnc_body']['prev_precedence_weights'] = [updated_precedence_weights]
        dnc_state_dict['dnc_body']['prev_read_weights'] = [new_read_weights]
        
        # External Output Function :
        ext_output = self.controller.forward_external_output_fn( 
            vt_output=vt,
            slots_read=read_vec,
        )

        dnc_state_dict['dnc_body']['prev_read_vec'] = [read_vec.reshape(batch_size, -1)]
        dnc_state_dict['dnc_memory']['memory'] = [written_memory_state]
        frame_states.update({'dnc':dnc_state_dict})
        
        return ext_output, frame_states 

    def get_feature_shape(self):
        return self.output_dim