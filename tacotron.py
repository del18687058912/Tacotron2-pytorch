import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils as utils
from hparams import hparams as hp
import numpy as np
from ZoneoutRNN import ZoneoutRNN
import math

class Tacotron(nn.Module):
    def __init__(self, encoder, decoder, postnet, PostCBHG=None, max_length=1000):
        super(Tacotron, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.postnet = postnet
        self.post_cbhg = PostCBHG
        self.max_length = max_length

    def forward(self, input_group, mel_group = None, linear_target=None, stop_token_target=None):
        '''

        :param input_group: (input_seqs, max_input_len) input_seqs: [batch_size, max_input_len]
        :param mel_group: (mel_target, max_target_len) mel_target: [batch_size,  max_target_len, num_mels*hp.outputs_per_step]
        :param linear_target: [batch_size, max_target_len, num_freq*hp.outputs_per_step]
        :param stop_token_target: [batch_size, max_target_len] Value is zero indicating that this time step is not the end.
        :return:
        '''

        input_seqs, max_input_len = input_group
        batch_size = input_seqs.size(0)
        if mel_group is not None:
            mel_target, max_target_len = mel_group
            max_target_len = max_target_len
        else:
            assert hp.use_gta_mode == False, 'if use_gta_mode == True, please provide with target'
            max_target_len = self.max_length

        if hp.use_gta_mode:
            assert self.training == True, 'When model is evaluating, you can\'t use gta_mode'
        if hp.use_linear_spec and self.training:
            assert linear_target is not None, 'When model is training and use_linear_spec is True, ' \
                                              'please apply linear target to calculate loss'
        if hp.use_stop_token and self.training:
            assert stop_token_target is not None, 'When model is training and use_stop_token is True, ' \
                                              'please apply stop token target to calculate loss'

        self.encoder.initialize(batch_size, max_input_len)
        encoder_outputs = self.encoder(input_seqs)
        self.decoder.attn.initialize(batch_size, max_input_len, encoder_outputs)
        decoder_inputs = torch.zeros(batch_size, 1, self.decoder.prenet_input_size)
        #initial decoder hidden state
        decoder_hidden = torch.zeros(self.decoder.decoder_lstm_layers, batch_size, self.decoder.decoder_lstm_units)
        decoder_cell_state = torch.zeros(self.decoder.decoder_lstm_layers, batch_size, self.decoder.decoder_lstm_units)
        decoder_outputs = torch.zeros(batch_size, max_target_len, self.decoder.decoder_output_size)
        self.postnet.initialize(self.decoder.decoder_output_size, max_target_len)
        stop_token_prediction = torch.zeros(batch_size, max_target_len, hp.outputs_per_step)

        for t in range(max_target_len):
            decoder_output, stop_token_output, decoder_hidden, decoder_cell_state = \
                self.decoder(decoder_inputs, decoder_hidden, decoder_cell_state)
            decoder_outputs[:, t, :] = torch.squeeze(decoder_output, 1)
            stop_token_prediction[:, t, :] = torch.squeeze(stop_token_output, 1)
            if self.training:
                if hp.teacher_forcing_schema == "full":
                    decoder_inputs = mel_target[:, t:t+1, :]
                elif hp.teacher_forcing_schema == "semi":
                    decoder_inputs = (
                        decoder_output + mel_target[:, t:t+1, :]
                    ) / 2
                elif hp.teacher_forcing_schema == "random":
                    if np.random.random() <= hp.teacher_forcing_ratio:
                        decoder_inputs = mel_target[:, t:t+1, :]
                    else:
                        decoder_inputs = decoder_output
            else:
                decoder_inputs = decoder_outputs[:, t:t+1, :]
                finished = torch.round(stop_token_output)
                if hp.stop_at_any:
                    finished = torch.sum(torch.sum(finished, 1) > 0) == batch_size
                else:
                    finished = torch.sum(torch.sum(finished, 1) == hp.outputs_per_step) == batch_size
                if finished:
                    break

        postnet_outputs = self.postnet(decoder_outputs)
        mel_outputs = decoder_outputs + postnet_outputs

        #calculate linear outputs
        if hp.use_linear_spec:
            self.post_cbhg.initialize(self.decoder.decoder_output_size, max_target_len)
            expand_outputs = self.post_cbhg(mel_outputs)
            linear_outputs = F.linear(expand_outputs, weight=torch.nn.init.normal_(torch.empty(hp.num_freq*hp.outputs_per_step, expand_outputs.shape[2])))

        #calculate losses
        if self.training:
            decoder_loss = F.mse_loss(decoder_outputs, mel_target)
            mel_loss = F.mse_loss(mel_outputs, mel_target)

            loss = decoder_loss + mel_loss

            if hp.use_linear_spec:
                linear_loss = F.mse_loss(linear_outputs, linear_target)
                loss += linear_loss

            if hp.use_stop_token:
                stop_token_loss = F.binary_cross_entropy(stop_token_prediction, stop_token_target, reduction='sum')
                loss += stop_token_loss

            return loss
        else:
            #model is eval
            return mel_outputs

