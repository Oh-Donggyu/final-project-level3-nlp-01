import math
import logging
from dataclasses import dataclass

import torch
import torch.nn as nn
import torch.nn.functional as F
from packaging import version
from torch.nn import init
from tensorly.tenalg import multi_mode_dot
from transformers.generation_utils import GenerationMixin
from transformers.file_utils import PushToHubMixin
from transformers.modeling_utils import (ModuleUtilsMixin,
                                         apply_chunking_to_forward, 
                                         find_pruneable_heads_and_indices,
                                         prune_linear_layer)
from transformers.models.bart.modeling_bart import BartEncoderLayer, BartDecoderLayer
from transformers.activations import gelu_new
from transformers.modeling_outputs import (
    BaseModelOutputWithPastAndCrossAttentions,
    BaseModelOutputWithPoolingAndCrossAttentions, 
    CausalLMOutputWithCrossAttentions
)


logger = logging.get_logger(__name__)

class TeacherWeightGroup:
    teacher_model: nn.Module = None

    @classmethod
    def set_network(cls, teacher_model: nn.Module):
        TeacherWeightGroup.teacher_model = teacher_model

    @classmethod
    def generate_weight_group(
        cls, 
        weight_class_name: str, 
        current_layer_index: int, 
        num_student_layers: int
    ):
        part, weight_class_name = weight_class_name.split(".", 1)
        weight_instances = list()

        if part == "encoder":
            for instance_name, instance in TeacherWeightGroup.teacher_model.encoder.named_parameters():
                if class_name in instance_name:
                    weight_instances.append(instance)

        elif part == "decoder":
            for instance_name, instance in TeacherWeightGroup.teacher_model.decoder.named_parameters():
                if class_name in instance_name:
                    weight_instances.append(instance)
            
        weight_instances = torch.stack(weight_instances)
        teacher_network_layers = weight_instances.size()[0]
        
        start = layer_index * int(teacher_network_layers / num_layers)
        end = (layer_index + 1) * int(teacher_network_layers / num_layers)
        return weight_instances[start:end]


class WeightGenerator(nn.Module):
    def __init__(
        self,
        weight_class_name: str,
        current_layer_index: int,
        num_student_layers: int,
        student_weight_in: int,
        student_weight_out: int
    ):
        self.subset = TeacherWeightGroup.generate_weight_group(
            weight_class_name, current_layer_index, num_student_layers
        )
        num_adjacent_layers, teacher_weight_out, teacher_weight_in = self.subset.shape

        # self.W_i = nn.Parameter(torch.empty(student_weight_in, teacher_weight_in))
        # self.W_o = nn.Parameter(torch.empty(student_weight_out, teacher_weight_out))
        self.W_l = nn.Parameter(torch.empty(1, num_adjacent_layers))
        self.W = nn.Parameter(torch.ones(student_weight_out, student_weight_in))
        self.B = nn.Parameter(torch.zeros(student_weight_out, student_weight_in))

        self.tanh = nn.Tanh()
        self.init_weights_()
    
    def init_weights_(self):
        init.xavier_uniform_(self.W_i)
        init.xavier_uniform_(self.W_o)
        init.xavier_uniform_(self.W_l)

    def forward(self) -> nn.Parameter :

        # mode dot operation of tensorly library
        # student_param = multi_mode_dot(self.subset, modes=[2, 1, 0], matrix_or_vec_list=[self.W_i, self.W_o, self.W_l])
        student_param = multi_mode_dot(self.subset, modes=[0], matrix_or_vec_list=[self.W_l])

        return self.tanh(student_param.squeeze(0)) * self.W + self.B


class BiasGenerator(nn.Module):
    def __init__(
        self,
        weight_class_name: str,
        current_layer_index: int,
        num_student_layers: int,
        student_out_features: int,
    ):

        self.subset = TeacherWeightGroup.generate_weight_group(
            weight_class_name, current_layer_index, num_student_layers
        )
        num_adjacent_layers, teacher_out_features = self.subset.shape

        # self.W_o = nn.Parameter(torch.empty(student_out_features, teacher_out_features))
        self.W_l = nn.Parameter(torch.empty(1, num_adjacent_layers))
        self.W = nn.Parameter(torch.ones(student_out_features))
        self.B = nn.Parameter(torch.zeros(student_out_features))

        self.tanh = nn.Tanh()
        self.init_weights_()
    
    def init_weights_(self):
        init.xavier_uniform_(self.W_o)
        init.xavier_uniform_(self.W_l)
    
    def forward(self) -> nn.Parameter :

        # mode dot operation of tensorly library
        # student_param = multi_mode_dot(self.subset, modes=[1, 0], matrix_or_vec_list=[self.W_o, self.W_l])
        student_param = multi_mode_dot(self.subset, modes=[0], matrix_or_vec_list=[self.W_l])

        return self.tanh(student_param.squeeze(0)) * self.W + self.B


class StudentGrafomerModel(nn.Module, ModuleUtilsMixin, GenerationMixin, PushToHubMixin):
    def __init__(self, cfg):
        super().__init__()

        encoder_config = cfg.encoder
        decoder_config = cfg.decoder
        self.encoder = StudentEncoderModel(encoder_config)
        self.decoder = StudentDecoderLMHeadModel(decoder_config)

        self.config = cfg.grafomer
        
        self.graft_module = StudentGraftAttentionModule()
    
    def get_encoder(self):
        return self.encoder
    
    def forward(

    ):
        pass


class StudentGraftAttentionModule(nn.Module):
    def __init__(self):

        self.graft_encoder = nn.ModuleList([BartEncoderLayer() for _ in range()])
        self.graft_decoder = nn.ModuleList([BartDecoderLayer() for _ in range()])
    
    def forward(
        self,
        encoder_hidden_states: torch.Tensor,
        ncoder_attention_mask: torch.Tensor,
        decoder_hidden_states: torch.Tensor=None, 
        decoder_attention_mask: torch.Tensor=None, 
        cross_attention_mask: torch.Tensor=None,
        output_attentions: bool = False, 
        cross_attn_head_mask=None, 
        use_cache=False, 
        head_mask=None,
    ):

        pass






class StudentLinear(nn.Module):
    def __init__(
        self, 
        weight_class_name: str, 
        current_layer_index: int, 
        num_student_layers: int,
        in_features: int, 
        out_features: int,
    ):

        self.weight_generator = WeightGenerator(
            weight_class_name = weight_class_name, 
            current_layer_index = current_layer_index, 
            num_student_layers = num_student_layers, 
            student_weight_in = in_features, 
            student_weight_out = out_features,
        )
        self.bias_generator = BiasGenerator(
            weight_class_name = weight_class_name, 
            current_layer_index = current_layer_index, 
            num_student_layers = num_student_layers, 
            student_out_features = out_features,
        )
            
    def forward(self, inputs: torch.Tensor) -> torch.Tensor :

        student_weight = self.weight_generator()
        student_bias = self.bias_generator()
        
        return F.linear(inputs, student_weight, student_bias)


class StudentMLP(nn.Module):
    def __init__(
        self, 
        current_layer_index,
        num_student_layers,
        config
    ):
        super().__init__()
        hidden_size = config["hidden_size"]
        intermediate_size = config["intermediate_size"]
        
        self.c_fc = StudentLinear(
            "decoder.mlp.c_fc", 
            current_layer_index, 
            num_student_layers, 
            hidden_size, 
            intermediate_size
        )
        self.c_proj = StudentLinear(
            "decoder.mlp.c_proj", 
            current_layer_index, 
            num_student_layers, 
            intermediate_size, 
            hidden_size
        )
        self.act = gelu_new
        self.dropout = nn.Dropout(config["resid_pdrop"])
    
    def forward(self, hidden_states):
        hidden_states = self.c_fc(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.c_proj(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class StudentDecoderAttention(nn.Module):
    def __init__(
        self, 
        current_layer_index: int,
        num_student_layers: int,
        config
    ):
        super().__init__()

        max_positions = config["max_position_embeddings"]
        self.register_buffer(
            "bias",
            torch.tril(torch.ones((max_positions, max_positions), dtype=torch.uint8)).view(
                1, 1, max_positions, max_positions
            ),
        )
        self.register_buffer("masked_bias", torch.tensor(-1e4))

        self.embed_dim = config["hidden_size"]
        self.num_heads = config["num_attention_heads"]
        self.head_dim = self.embed_dim // self.num_heads
        self.split_size = self.embed_dim
        if self.head_dim * self.num_heads != self.embed_dim:
            raise ValueError(
                f"`embed_dim` must be divisible by num_heads (got `embed_dim`: {self.embed_dim} and `num_heads`: {self.num_heads})."
            )
        
        self.scale_attn_weights = config["scale_attn_weights"]

        self.c_attn = StudentLinear(
            "decoder.attn.c_attn", 
            current_layer_index, 
            num_student_layers, 
            self.embed_dim, 
            3 * self.embed_dim
        )
        self.c_proj = StudentLinear(
            "decoder.attn.c_proj", 
            current_layer_index, 
            num_student_layers, 
            self.embed_dim, 
            self.embed_dim
        )
        self.attn_dropout = nn.Dropout(config["attn_pdrop"])
        self.resid_drop = nn.Dropout(config["resid_pdrop"])

        self.prune_heads = set()
    
    def prune_heads(self, heads):
        pass

    def _attn(self, query, key, value, attention_mask=None, head_mask=None):
        attn_weights = torch.matmul(query, key.transpose(-1, -2))

        if self.scale_attn_weights:
            attn_weights = attn_weights / (float(value.size(-1)) ** 0.5)
        
        query_length, key_length = query.size(-2), key.size(-2)
        causal_mask = self.bias[:, :, key_length - query_length : key_length, :key_length].bool()
        attn_weights = torch.where(causal_mask, attn_weights, self.masked_bias.to(attn_weights.dtype))

        if attention_mask is not None:
            # Apply the attention mask
            attn_weights = attn_weights + attention_mask

        attn_weights = nn.Softmax(dim=-1)(attn_weights)
        attn_weights = self.attn_dropout(attn_weights)

        # Mask heads if we want to
        if head_mask is not None:
            attn_weights = attn_weights * head_mask

        attn_output = torch.matmul(attn_weights, value)

        return attn_output, attn_weights
    
    def _split_heads(self, tensor, num_heads, attn_head_size):
        """
        Splits hidden_size dim into attn_head_size and num_heads
        """
        new_shape = tensor.size()[:-1] + (num_heads, attn_head_size)
        tensor = tensor.view(*new_shape)
        return tensor.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)

    def _merge_heads(self, tensor, num_heads, attn_head_size):
        """
        Merges attn_head_size dim and num_attn_heads dim into hidden_size
        """
        tensor = tensor.permute(0, 2, 1, 3).contiguous()
        new_shape = tensor.size()[:-2] + (num_heads * attn_head_size,)
        return tensor.view(new_shape)
    
    def forward(
        self,
        hidden_states,
        layer_past=None,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=False,
        output_attentions=False,
    ):

        query, key, value = self.c_attn(hidden_states).split(self.split_size, dim=2)

        query = self._split_heads(query, self.num_heads, self.head_dim)
        key = self._split_heads(key, self.num_heads, self.head_dim)
        value = self._split_heads(value, self.num_heads, self.head_dim)

        if layer_past is not None:
            past_key, past_value = layer_past
            key = torch.cat((past_key, key), dim=-2)
            value = torch.cat((past_value, value), dim=-2)
        
        if use_cache is True:
            present = (key, value)
        else:
            present = None
        
        attn_output, attn_weights = self._attn(query, key, value, attention_mask, head_mask)

        attn_output = self._merge_heads(attn_output, self.num_heads, self.head_dim)
        attn_output = self.c_proj(attn_output)
        attn_output = self.resid_dropout(attn_output)

        outputs = (attn_output, present)
        if output_attentions:
            outputs += (attn_weights,)

        return outputs  # a, present, (attentions)


class StudentDecoderBlock(nn.Module):
    def __init__(
        self, 
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):

        hidden_size = config["hidden_size"]
        inner_dim = config["n_inner"] if "n_inner" in config.keys() else 4 * self.hidden_size

        self.ln_1 = nn.LayerNorm(hidden_size, eps=config["layer_norm_epsilon"])
        self.attn = StudentDecoderAttention(
            current_layer_index,
            num_student_layers,
            config,
        )
        self.ln_2 = nn.LayerNorm(hidden_size, eps=config["layer_norm_epsilon"])
        self.mlp = StudentMLP(
            current_layer_index, 
            num_student_layers, 
            config
        )
    
    def forward(
        self,
        hidden_states,
        layer_past=None,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=False,
        output_attentions=False,
    ):
        residual = hidden_states
        hidden_states = self.ln_1(hidden_states)
        attn_outputs = self.attn(
            hidden_states,
            layer_past=layer_past,
            attention_mask=attention_mask,
            head_mask=head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        attn_output = attn_outputs[0]  # output_attn: a, present, (attentions)
        outputs = attn_outputs[1:]
        # residual connection
        hidden_states = attn_output + residual

        residual = hidden_states
        hidden_states = self.ln_2(hidden_states)
        feed_forward_hidden_states = self.mlp(hidden_states)
        # residual connection
        hidden_states = residual + feed_forward_hidden_states

        if use_cache:
            outputs = (hidden_states,) + outputs
        else:
            outputs = (hidden_states,) + outputs[1:]

        return outputs  # hidden_states, present, (attentions, cross_attentions)


class StudentDecoderModel(nn.Module, ModuleUtilsMixin, GenerationMixin, PushToHubMixin):
    
    def __init__(self, config):
        super().__init__()
        
        self.embed_dim = config["hidden_size"]
        self.vocab_size = configp["vocab_size"]
        self.max_position_embeddings = config["max_position_embeddings"]
        self.embed_pdrop = config["embed_pdrop"]
        self.num_hidden_layers = config["num_decoder_layers"]
        
        self.wte = nn.Embedding(self.vocab_size, self.embed_dim, padding_idx=config["pad_token_id"])
        self.wpe = nn.Embedding(self.max_position_embeddings, self.embed_dim)

        self.drop = nn.Dropout(self.embed_pdrop)
        self.h = nn.ModuleList(
            [StudentDecoderBlock(i, self.num_hidden_layers, config) for i in range(self.num_hidden_layers)]
        )
        self.ln_f = nn.LayerNorm(self.embed_dim, eps=config["layer_norm_epsilon"])

        # self.init_weights()

        # Model parallel
        self.model_parallel = False
        self.device_map = None
        self.gradient_checkpointing = False

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, new_embeddings):
        self.wte = new_embeddings
    
    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
        """
        for layer, heads in heads_to_prune.items():
            self.h[layer].attn.prune_heads(heads)    
    
    def forward(
        self,
        input_ids=None,
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        # output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
            batch_size = input_ids.shape[0]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
            batch_size = inputs_embeds.shape[0]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if token_type_ids is not None:
            token_type_ids = token_type_ids.view(-1, input_shape[-1])
        if position_ids is not None:
            position_ids = position_ids.view(-1, input_shape[-1])
        
        if past_key_values is None:
            past_length = 0
            past_key_values = tuple([None] * len(self.h))
        else:
            past_length = past_key_values[0][0].size(-2)
        
        if position_ids is None:
            position_ids = torch.arange(past_length, input_shape[-1] + past_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).view(-1, input_shape[-1])
        
        # GPT2Attention mask
        if attention_mask is not None:
            if batch_size <= 0:
                raise ValueError("batch_size has to be defined and > 0")
            attention_mask = attention_mask.view(batch_size, -1)
            # We create a 3D attention mask from a 2D tensor mask.
            # Sizes are [batch_size, 1, 1, to_seq_length]
            # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
            # this attention mask is more simple than the triangular masking of causal attention
            # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
            attention_mask = attention_mask[:, None, None, :]

            attention_mask = attention_mask.to(dtype=self.dtype)  # fp16 compatibility
            attention_mask = (1.0 - attention_mask) * -10000.0

        # we don't need cross attention
        encoder_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # head_mask has shape n_layer x batch x n_heads x N x N
        head_mask = self.get_head_mask(head_mask, self.num_hidden_layers)

        if inputs_embeds is None:
            inputs_embeds = self.wte(input_ids)
        position_embeds = self.wpe(position_ids)
        hidden_states = inputs_embeds + position_embeds

        # if token_type_ids is not None:
        #     token_type_embeds = self.wte(token_type_ids)
        #     hidden_states = hidden_states + token_type_embeds

        hidden_states = self.drop(hidden_states)

        output_shape = input_shape + (hidden_states.size(-1),)

        presents = () if use_cache else None
        all_self_attentions = () if output_attentions else None
        # all_cross_attentions = () if output_attentions and self.config.add_cross_attention else None
        all_hidden_states = () if output_hidden_states else None
        for i, (block, layer_past) in enumerate(zip(self.h, past_key_values)):

            # Model parallel
            if self.model_parallel:
                torch.cuda.set_device(hidden_states.device)
                # Ensure layer_past is on same device as hidden_states (might not be correct)
                if layer_past is not None:
                    layer_past = tuple(past_state.to(hidden_states.device) for past_state in layer_past)
                # Ensure that attention_mask is always on the same device as hidden_states
                if attention_mask is not None:
                    attention_mask = attention_mask.to(hidden_states.device)
                if isinstance(head_mask, torch.Tensor):
                    head_mask = head_mask.to(hidden_states.device)
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)
            
            if self.gradient_checkpointing and self.training:
 
                if use_cache:
                    logger.warning(
                        "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                    )
                    use_cache = False
                
                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, use_cache, output_attentions)

                    return custom_forward
                
                outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    None,
                    attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                outputs = block(
                    hidden_states,
                    layer_past = layer_past,
                    attention_mask = attention_mask,
                    head_mask = head_mask[i],
                    encoder_hidden_states = encoder_hidden_states,
                    encoder_attention_mask = encoder_attention_mask,
                    use_cache = use_cache,
                    output_attentions = output_attentions,
                )
            
            hidden_states = outputs[0]
            if use_cache is True:
                presents = presents + (outputs[1],)

            if output_attentions:
                all_self_attentions = all_self_attentions + (outputs[2 if use_cache else 1],)
                # if self.config.add_cross_attention:
                #     all_cross_attentions = all_cross_attentions + (outputs[3 if use_cache else 2],)

            # Model Parallel: If it's the last layer for that device, put things on the next device
            if self.model_parallel:
                for k, v in self.device_map.items():
                    if i == v[-1] and "cuda:" + str(k) != self.last_device:
                        hidden_states = hidden_states.to("cuda:" + str(k + 1))
        
        hidden_states = self.ln_f(hidden_states)

        hidden_states = hidden_states.view(*output_shape)
        # Add last hidden state
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, presents, all_hidden_states, all_self_attentions, all_cross_attentions]
                if v is not None
            )

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )


class StudentDecoderLMHeadModel(nn.Module, ModuleUtilsMixin, GenerationMixin, PushToHubMixin):
    
    def __init__(self, conifg):
        super().__init__()
        self.transformer = StudentDecoderModel(config)
        self.lm_head = nn.Linear(config["n_embd"], config["vocab_size"])

        self.init_weights()

        self.model_parallel = False
        self.device_map = None

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def prepare_inputs_for_generation(self, input_ids, past=None, **kwargs):
        token_type_ids = kwargs.get("token_type_ids", None)
        # only last token for inputs_ids if past is defined in kwargs
        if past:
            input_ids = input_ids[:, -1].unsqueeze(-1)
            if token_type_ids is not None:
                token_type_ids = token_type_ids[:, -1].unsqueeze(-1)
        
        attention_mask = kwargs.get("attention_mask", None)
        position_ids = kwargs.get("position_ids", None)

        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past:
                position_ids = position_ids[:, -1].unsqueeze(-1)
        else:
            position_ids = None
        return {
            "input_ids": input_ids,
            "past_key_values": past,
            "use_cache": kwargs.get("use_cache"),
            "position_ids": position_ids,
        }
    def init_weights(self):
        self.apply(self._init_weights)
        self.tie_weights()

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, (nn.Linear, Conv1D)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
    
    def forward(
        self,
        input_ids=None, 
        past_key_values=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for language modeling. Note that the labels **are shifted** inside the model, i.e. you can set
            ``labels = input_ids`` Indices are selected in ``[-100, 0, ..., config.vocab_size]`` All labels set to
            ``-100`` are ignored (masked), the loss is only computed for labels in ``[0, ..., config.vocab_size]``
        """
        transformer_outputs = self.transformer(
            input_ids,
            past_key_values = past_key_values,
            attention_mask = attention_mask,
            token_type_ids = token_type_ids,
            position_ids = position_ids,
            head_mask = head_mask,
            inputs_embeds = inputs_embeds,
            encoder_hidden_states = encoder_hidden_states,
            encoder_attention_mask = encoder_attention_mask,
            use_cache = use_cache,
            output_attentions = output_attentions,
            output_hidden_states = output,
            return_dict = return_dict
        )
        hidden_states = transformer_outputs[0]

        # Set device for model parallelism (we don't need)
        # if self.model_parallel:
        #     torch.cuda.set_device(self.transformer.first_device)
        #     hidden_states = hidden_states.to(self.lm_head.weight.device)

        lm_logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            # shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
        
        if not return_dict:
            output = (lm_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output
        
        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=lm_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
            cross_attentions=transformer_outputs.cross_attentions,
        )
        
    @staticmethod
    def _reorder_cache(past: Tuple[Tuple[torch.Tensor]], beam_idx: torch.Tensor) -> Tuple[Tuple[torch.Tensor]]:
        """
        This function is used to re-order the :obj:`past_key_values` cache if
        :meth:`~transformers.PreTrainedModel.beam_search` or :meth:`~transformers.PreTrainedModel.beam_sample` is
        called. This is required to match :obj:`past_key_values` with the correct beam_idx at every generation step.
        """
        return tuple(
            tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past)
            for layer_past in past
        )
    
    def tie_weights(self):
        """
        Tie the weights between the input embeddings and the output embeddings.

        If the :obj:`torchscript` flag is set in the configuration, can't handle parameter sharing so we are cloning
        the weights instead.
        """
        output_embeddings = self.get_output_embeddings()
        if output_embeddings is not None and self.config.tie_word_embeddings:
            self._tie_or_clone_weights(output_embeddings, self.get_input_embeddings())

        if self.config.is_encoder_decoder and self.config.tie_encoder_decoder:
            if hasattr(self, self.base_model_prefix):
                self = getattr(self, self.base_model_prefix)
            self._tie_encoder_decoder_weights(self.encoder, self.decoder, self.base_model_prefix)

        for module in self.modules():
            if hasattr(module, "_tie_weights"):
                module._tie_weights()
    
    def _tie_or_clone_weights(self, output_embeddings, input_embeddings):
        """Tie or clone module weights depending of whether we are using TorchScript or not"""
        if self.config.torchscript:
            output_embeddings.weight = nn.Parameter(input_embeddings.weight.clone())
        else:
            output_embeddings.weight = input_embeddings.weight

        if getattr(output_embeddings, "bias", None) is not None:
            output_embeddings.bias.data = nn.functional.pad(
                output_embeddings.bias.data,
                (
                    0,
                    output_embeddings.weight.shape[0] - output_embeddings.bias.shape[0],
                ),
                "constant",
                0,
            )
        if hasattr(output_embeddings, "out_features") and hasattr(input_embeddings, "num_embeddings"):
            output_embeddings.out_features = input_embeddings.num_embeddings


class EncoderEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings."""
    def __init__(self, config):
        super().__init__()

        self.vocab_size = config["vocab_size"]
        self.max_position_embeddings = config["max_position_embeddings"]
        self.type_vocab_size = config["type_vocab_size"]
        self.pad_token_id = config["pad_token_id"]
        self.hidden_size = config["hidden_size"]

        self.word_embeddings = nn.Embedding(self.vocab_size, self.hidden_size, padding_idx=self.pad_token_id)
        self.position_embeddings = nn.Embedding(self.max_position_embeddings, self.hidden_size)
        self.token_type_embeddings = nn.Embedding(self.type_vocab_size, self.hidden_size)

        self.LayerNorm = nn.LayerNorm(self.hidden_size, eps=config["layer_norm_eps"])
        self.dropout = nn.Dropout(config["hidden_dropout_prob"])
        # position_ids (1, len position emb) is contiguous in memory and exported when serialized
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        self.register_buffer("position_ids", torch.arange(config["max_position_embeddings"]).expand(1, -1))
        if version.parse(torch.__version__) > version.parse("1.6.0"):
            self.register_buffer(
                "token_type_ids", 
                torch.zeros(self.position_ids.size(), dtype=torch.long, device=self.position_ids.device),
            )
    
    def forward(
        self, input_ids=None, 
        token_type_ids=None, 
        position_ids=None, 
        inputs_embeds=None, 
        past_key_values_length=0
    ):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]
        
        seq_length = input_shape[1]

        if position_ids is None:
            position_ids = self.position_ids[:, past_key_values_length : seq_length + past_key_values_length]

        # Setting the token_type_ids to the registered buffer in constructor where it is all zeros, which usually occurs
        # when its auto-generated, registered buffer helps users when tracing the model without passing token_type_ids, solves
        # issue #5664
        if token_type_ids is None:
            if hasattr(self, "token_type_ids"):
                buffered_token_type_ids = self.token_type_ids[:, :seq_length]
                buffered_token_type_ids_expanded = buffered_token_type_ids.expand(input_shape[0], seq_length)
                token_type_ids = buffered_token_type_ids_expanded
            else:
                token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=self.position_ids.device)
        
        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = inputs_embeds + token_type_embeddings
        if self.position_embedding_type == "absolute":
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class StudentEncoderSelfAttention(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        super().__init__()
        if config["hidden_size"] % config["num_attention_heads"] != 0 and not hasattr(config, "emebdding_size"):
            raise ValueError(
                f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
                f"heads ({config.num_attention_heads})"
            )
        
        self.hidden_size = config["hidden_size"]
        self.num_attention_heads = config["num_attention_heads"]
        self.attention_head_size = int(self.hidden_size / config["num_attention_heads"])
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = StudentLinear(
            "encoder.attention.self.query",
            current_layer_index, 
            num_student_layers, 
            self.hidden_size, 
            self.all_head_size,
        )
        self.key = StudentLinear(
            "encoder.attention.self.key", 
            current_layer_index, 
            num_student_layers, 
            self.hidden_size, 
            self.all_head_size,
        )
        self.value = StudentLinear(
            "encoder.attention.self.value", 
            current_layer_index, 
            num_student_layers, 
            self.hidden_size, 
            self.all_head_size,
        )

        self.dropout = nn.Dropout(config["attention_probs_dropout_prob"])
        self.position_embedding_type = getattr(config, "position_embedding_type", "absolute")
        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            self.max_position_embeddings = config["max_position_embeddings"]
            self.distance_embedding = nn.Embedding(2 * self.max_position_embeddings - 1, self.attention_head_size)
        
        # self.is_decoder = config["is_decoder"]

    def transpose_for_scores(self, x):
        new_x_type = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_type)
        return x.permute(0, 2, 1, 3)
    
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        mixed_query_layer = self.query(hidden_states)

        # (we don't need)
        # is_cross_attention = encoder_hidden_states is not None 
        # (...)
        # (transformers.models.bert.modeling_bert line 274 참조)

        if past_key_value is not None:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))
            key_layer = torch.cat([past_key_value[0], key_layer], dim=2)
            value_layer = torch.cat([past_key_value[1], value_layer], dim=2)
        else:
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            value_layer = self.transpose_for_scores(self.value(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)

        # if self.is_decoder:
            # (...)
            # (we don't need)

        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))

        if self.position_embedding_type == "relative_key" or self.position_embedding_type == "relative_key_query":
            seq_length = hidden_states.size()[1]
            position_ids_l = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(-1, 1)
            position_ids_r = torch.arange(seq_length, dtype=torch.long, device=hidden_states.device).view(1, -1)
            distance = position_ids_l - position_ids_r
            positional_embedding = self.distance_embedding(distance + self.max_position_embeddings - 1)
            positional_embedding = positional_embedding.to(dtype=query_layer.dtype)

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum("bhld,lrd->bhlr", query_layer, positional_embedding)
                relative_position_scores_key = torch.einsum("bhrd,lrd->bhlr", key_layer, positional_embedding)
                attention_scores = attention_scores + relative_position_scores_query + relative_position_scores_key

        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask
        
        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)

        # if self.is_decoder:
        #     outputs = outputs + (past_key_value,)
        return outputs


class EncoderSelfOutput(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        self.dense = StudentLinear(
            "encoder.attention.output.dense",
            current_layer_index, 
            num_student_layers, 
            config["hidden_size"],
            config["hidden_size"],
        )
        self.LayerNorm = nn.LayerNorm(config["hidden_size"], eps=config["layer_norm_eps"])
        self.dropout = nn.Dropout(config["hidden_dropout_prob"])
    
    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class StudentEncoderAttention(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        self.self = StudentEncoderSelfAttention(
            current_layer_index, 
            num_student_layers, 
            config,
        )
        self.output = EncoderSelfOutput(
            current_layer_index, 
            num_student_layers, 
            config,
        )
        self.prune_heads = set()
    
    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.self.num_attention_heads, self.self.attention_head_size, self.pruned_heads
        )

        # Prune linear layers
        self.self.query = prune_linear_layer(self.self.query, index)
        self.self.key = prune_linear_layer(self.self.key, index)
        self.self.value = prune_linear_layer(self.self.value, index)
        self.output.dense = prune_linear_layer(self.output.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.self.num_attention_heads = self.self.num_attention_heads - len(heads)
        self.self.all_head_size = self.self.attention_head_size * self.self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)
    
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            past_key_value,
            output_attentions,
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


class StudentEncoderIntermediate(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        super().__init__()
        self.dense = StudentLinear(
            "encoder.intermediate.dense", 
            current_layer_index,
            num_student_layers, 
            config["hidden_size"], 
            config["intermediate_size"],
        )
        self.intermediate_act_fn = nn.functional.gelu
    
    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class StudentEncoderOutput(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        super().__init__()
        self.dense = StudentLinear(
            "encoder.output.dense", 
            current_layer_index, 
            num_student_layers, 
            config["intermediate_size"], 
            config["hidden_size"],
        )
        self.LayerNorm = nn.LayerNorm(config["hidden_size"], eps=config["layer_norm_eps"])
        self.dropout = nn.Dropout(config["hidden_dropout_prob"])

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states
    

class StudentEncoderLayer(nn.Module):
    def __init__(
        self,
        current_layer_index: int,
        num_student_layers: int,
        config,
    ):
        super().__init__()
        self.chunk_size_feed_forward = config["chunk_size_feed_forward"]
        self.seq_len_dim = 1
        self.attention = StudentEncoderAttention(
            current_layer_index,
            num_student_layers,
            config,
        )
        # self.is_decoder = config["is_decoder"]
        # self.add_cross_attention = config["add_cross_attention"]
        # (we don't need)
        self.intermediate = StudentEncoderIntermediate(
            current_layer_index,
            num_student_layers,
            config,
        )
        self.output = StudentEncoderOutput(
            current_layer_index,
            num_student_layers,
            config,
        )
    
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        output_attentions=False,
    ):
        # decoder uni-directional self-attention cached key/values tuple is at positions 1,2
        self_attn_past_key_value = past_key_value[:2] if past_key_value is not None else None
        self_attention_outputs = self.attention(
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions=output_attentions,
            past_key_value=self_attn_past_key_value,
        )
        attention_output = self_attention_outputs[0]

        # if decoder, the last outputs is tuple of self-attn cache
        # if self.is_decoder:
        #     outputs = self_attention_outputs[1:-1]
        #     present_key_value = self_attention_outputs[-1]
        outputs = self_attention_outputs[1:]

        cross_attn_present_key_value = None
        # ... is decoder and encoder hidden states is not None ...
        # ... skip ...

        layer_output = apply_chunking_to_forward(
            self.feed_forward_chunk, self.chunk_size_feed_forward, self.seq_len_dim, attention_output
        )
        outputs = (layer_output,) + outputs

        return outputs

    def feed_forward_chunk(self, attention_output):
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output


class StudentEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.num_hidden_layers = config["num_hidden_layers"]

        self.layer = nn.ModuleList(
            [StudentEncoderLayer(i, self.num_hidden_layers, config) for i in range(self.num_hidden_layers)]
        )
        self.gradient_checkpointing = False

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=True,
    ):
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None
        # all_cross_attentions = () if output_attentions and self.config.add_cross_attention else None

        next_decoder_cache = () if use_cache else None
        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)
            
            layer_head_mask = head_mask[i] if head_mask is not None else None
            past_key_value = past_key_values[i] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                if use_cache:
                    logger.warning(
                        "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                    )
                    use_cache = False

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs, past_key_value, output_attentions)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(layer_module),
                    hidden_states,
                    attention_mask,
                    layer_head_mask,
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                layer_outputs = layer_module(
                    hidden_states,
                    attention_mask,
                    layer_head_mask,
                    encoder_hidden_states,
                    encoder_attention_mask,
                    past_key_value,
                    output_attentions,
                )
            
            hidden_states = layer_outputs[0]
            if use_cache:
                next_decoder_cache += (layer_outputs[-1],)
            if output_attentions:
                all_self_attentions = all_self_attentions + (layer_outputs[1],)
                if self.config.add_cross_attention:
                    all_cross_attentions = all_cross_attentions + (layer_outputs[2],)
        
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)
        
        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    next_decoder_cache,
                    all_hidden_states,
                    all_self_attentions,
                    all_cross_attentions,
                ]
                if v is not None
            )
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=next_decoder_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )


class EncoderPooler(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config["hidden_size"], config["hidden_size"])
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class StudentEncoderModel(nn.Module, ModuleUtilsMixin, GenerationMixin, PushToHubMixin):
    def __init__(self, config, add_pooling_layer=True):
        self.config = config

        self.embeddings = EncoderEmbeddings(config)
        self.encoder = StudentEncoder(config)

        self.pooler = EncoderPooler(config) if add_pooling_layer else None

        self.init_weights()

    def init_weights(self):

        self.apply(self._init_weights)
        # self.tie_weigths()
    
    def _init_weights(self, module):
        """Initialize the weights"""
        if isinstance(module, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
    
    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value
    
    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)
    
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        if input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")
        
        batch_size, seq_length = input_shape
        device = input_ids.device if input_ids is not None else inputs_embeds.device

        # past_key_values_length
        past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0

        if attention_mask is None:
            attention_mask = torch.ones(((batch_size, seq_length + past_key_values_length)), device=device)

        if token_type_ids is None:
            if hasattr(self.embeddings, "token_type_ids"):
                buffered_token_type_ids = self.embeddings.token_type_ids[:, :seq_length]
                buffered_token_type_ids_expanded = buffered_token_type_ids.expand(batch_size, seq_length)
                token_type_ids = buffered_token_type_ids_expanded
            else:
                token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)
        
        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        extended_attention_mask: torch.Tensor = self.get_extended_attention_mask(attention_mask, input_shape, device)

        # (is_decoder... skip ..)
        encoder_extended_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)

        embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            inputs_embeds=inputs_embeds,
            past_key_values_length=past_key_values_length,
        )
        encoder_outputs = self.encoder(
            embedding_output,
            attention_mask=extended_attention_mask,
            head_mask=head_mask,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_extended_attention_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        sequence_output = encoder_outputs[0]
        pooled_output = self.pooler(sequence_output) if self.pooler is not None else None

        if not return_dict:
            return (sequence_output, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPoolingAndCrossAttentions(
            last_hidden_state=sequence_output,
            pooler_output=pooled_output,
            past_key_values=encoder_outputs.past_key_values,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
            cross_attentions=encoder_outputs.cross_attentions,
        )
