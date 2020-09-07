from typing import Dict, List, Optional

from functools import partial 

import torch

from regym.rl_algorithms.algorithms import Algorithm 

eps = 1e-4

def value_function_rescaling(x):
    '''
    Value function rescaling (table 2).
    '''
    return torch.sign(x) * (torch.sqrt(torch.abs(x) + 1.) - 1.) + eps * x


def inverse_value_function_rescaling(x):
    '''
    See Proposition A.2 in paper "Observe and Look Further".
    '''
    return torch.sign(x) * (
        (
            (torch.sqrt(1. + 4. * eps * (torch.abs(x) + 1. + eps)) - 1.) / (2. * eps)
        ).pow(2.0) - 1.
    )


def extract_rnn_states_from_time_indices(rnn_states_batched: Dict, 
                                         time_indices_start:int, 
                                         time_indices_end:int):
    if rnn_states_batched is None:  return None 

    rnn_states = {k: {} for k in rnn_states_batched}
    for recurrent_submodule_name in rnn_states_batched:
        if 'hidden' in rnn_states_batched[recurrent_submodule_name]:
            tis=time_indices_start
            tie=time_indices_end
            squeeze_needed= False
            if tis==tie:    
                tie+=1
                squeeze_needed = True
            rnn_states[recurrent_submodule_name] = {'hidden':[], 'cell':[]}
            for idx in range(len(rnn_states_batched[recurrent_submodule_name]['hidden'])):
                hidden = rnn_states_batched[recurrent_submodule_name]['hidden'][idx][:, tis:tie,...]
                if squeeze_needed:  hidden = hidden.squeeze(1) 
                rnn_states[recurrent_submodule_name]['hidden'].append(hidden)
                if 'cell' in rnn_states_batched[recurrent_submodule_name]:
                    cell = rnn_states_batched[recurrent_submodule_name]['cell'][idx][:, tis:tie,...]
                    if squeeze_needed:  cell = cell.squeeze(1)
                    rnn_states[recurrent_submodule_name]['cell'].append(cell)
        else:
            rnn_states[recurrent_submodule_name] = extract_rnn_states_from_time_indices(
                rnn_states_batched=rnn_states_batched[recurrent_submodule_name], 
                time_indices_start=time_indices_start, 
                time_indices_end=time_indices_end, 
            )
    return rnn_states


def replace_rnn_states_at_time_indices(rnn_states_batched: Dict, 
                                       replacing_rnn_states_batched: Dict, 
                                       time_indices_start:int, 
                                       time_indices_end:int):
    if rnn_states_batched is None:  return None 

    rnn_states = {k: {} for k in rnn_states_batched}
    for recurrent_submodule_name in rnn_states_batched:
        if 'hidden' in rnn_states_batched[recurrent_submodule_name]:
            rnn_states[recurrent_submodule_name] = {'hidden':[], 'cell':[]}
            for idx in range(len(rnn_states_batched[recurrent_submodule_name]['hidden'])):
                hidden = rnn_states_batched[recurrent_submodule_name]['hidden'][idx] 
                batch_size = hidden.shape[0]
                unroll_size = time_indices_end+1-time_indices_start 
                hidden[:, time_indices_start:time_indices_end+1,...] = replacing_rnn_states_batched[recurrent_submodule_name]['hidden'][idx].reshape(batch_size, unroll_size, -1)
                rnn_states[recurrent_submodule_name]['hidden'].append(hidden)
                if 'cell' in rnn_states_batched[recurrent_submodule_name]:
                    cell = rnn_states_batched[recurrent_submodule_name]['cell'][idx] 
                    cell[:, time_indices_start:time_indices_end+1,...] = replacing_rnn_states_batched[recurrent_submodule_name]['cell'][idx].reshape(batch_size, unroll_size, -1)
                    rnn_states[recurrent_submodule_name]['cell'].append(cell)
        else:
            rnn_states[recurrent_submodule_name] = replace_rnn_states_at_time_indices(
                rnn_states_batched=rnn_states_batched[recurrent_submodule_name], 
                replacing_rnn_states_batched=replacing_rnn_states_batched[recurrent_submodule_name], 
                time_indices_start=time_indices_start, 
                time_indices_end=time_indices_end, 
            )

    return rnn_states


def roll_sequences(unrolled_sequences:List[Dict[str, torch.Tensor]], batch_size:int=1):
    '''
    Returns a dictionnary of torch tensors from the list of dictionnaries `unrolled_sequences`. 
    '''
    keys = unrolled_sequences[0].keys()
    d = {}
    for key in keys:
        # (batch_size=1, unroll_dim, ...)
        if isinstance(unrolled_sequences[0][key], dict):
            values = [unrolled_sequences[i][key] for i in range(len(unrolled_sequences))]
            value = Algorithm._concatenate_hdict(
                values.pop(0), 
                values, 
                map_keys=['hidden', 'cell'], 
                concat_fn=partial(torch.cat, dim=1),   # concatenate on the unrolling dimension (axis=1).
                preprocess_fn=(lambda x: x.reshape(batch_size, 1, -1)), # backpropagate through time
                #preprocess_fn=(lambda x: x.reshape(batch_size, 1, -1).detach()),   # truncated?
            )
        else: 
            value = torch.cat(
                [
                    unrolled_sequences[i][key].reshape(batch_size, 1, -1)    # add unroll dim 
                    for i in range(len(unrolled_sequences)) 
                ],
                dim=1
            )
        d[key] = value
    return d


def unrolled_inferences(model: torch.nn.Module, 
                        states: torch.Tensor, 
                        rnn_states: Dict[str, Dict[str, List[torch.Tensor]]],
                        goals: torch.Tensor=None,
                        grad_enabler: bool=False,
                        use_zero_initial_states: bool=False,
                        extras: bool=False):
    '''
    Compute feed-forward inferences on the :param model: of the :param states: with the rnn_states used as burn_in values.
    NOTE: The function also computes the inferences using the rnn states used when gathering the states, in order to 
    later allow a computation of the Q-value discrepency $$\Delta Q$$ (see end of page 4).
    
    :param model: torch.nn.Module to use for inference.
    :param states: torch.Tensor of shape (batch_size, unroll_dim, ...) to use as input for inference.
    :param rnn_states: Hierarchy of dictionnaries containing as leaf the hidden an cell states of the relevant recurrent modules.
                        The shapes are batch_first, i.e. (batch_size, unroll_dim, ...).
    :param goals: Dimension batch_size x goal shape: Goal of the agent.
    :param grad_enable: boolean specifying whether to compute gradient.
    :param use_zero_initial_states: boolean specifying whether the initial recurrent states are zeroed or sampled from the unrolled sequence.

    :return burn_in_predictions: Dict of outputs produced by the :param model: with shape (batch_size, unroll_dim, ...),
                                    when the recurrent cell states are burned in throughout the unrolled sequence, 
                                    with the exception of the first one, which can be zeroed if :param use_zero_initial_states:
                                    is `True`, otherwise it is sampled from the unrolle sequence.
    :return unrolled_predictions: Dict of outputs produced by the :param model: with shape (batch_size, unroll_dim, ...),
                                    when the recurrent cell states are sampled from the unrolled sequence.
    :return burned_in_rnn_states_inputs: Hierarchy of dictionnaries containing the final hidden and cell states of the recurrent
                                        submodules contained in :param model:, with shape (batch_size, unroll_dim=1, ...).
    '''
    batch_size = states.shape[0]
    unroll_length = states.shape[1]

    init_rnn_states_inputs = None
    if not use_zero_initial_states: 
        init_rnn_states_inputs = extract_rnn_states_from_time_indices(
            rnn_states, 
            time_indices_start=0,
            time_indices_end=0
        )

    burn_in_predictions =  []
    unrolled_predictions =  []
    
    burn_in_rnn_states_inputs = init_rnn_states_inputs
    unrolled_rnn_states_inputs = init_rnn_states_inputs

    unrolled_prediction = None
    with torch.set_grad_enabled(grad_enabler):
        for unroll_id in range(unroll_length):
            inputs = states[:, unroll_id,...]
            
            burn_in_prediction = model(inputs, rnn_states=burn_in_rnn_states_inputs)
            
            if extras:
                unrolled_prediction = model(inputs, rnn_states=unrolled_rnn_states_inputs)
            
            burn_in_predictions.append(burn_in_prediction)
            if extras:
                unrolled_predictions.append(unrolled_prediction)

            # Bookkeeping: update the rnn states:
            burn_in_rnn_states_inputs = burn_in_prediction['next_rnn_states']
            if extras and unroll_id < unroll_length-1:
                unrolled_rnn_states_inputs = extract_rnn_states_from_time_indices(
                    rnn_states, 
                    time_indices_start=unroll_id+1,  #sample for next step...
                    time_indices_end=unroll_id+1,
                )

    burned_in_rnn_states_inputs = burn_in_rnn_states_inputs
    # (batch_size, ...)  
    burn_in_predictions = roll_sequences(burn_in_predictions, batch_size=batch_size)
    if extras:
        unrolled_predictions = roll_sequences(unrolled_predictions, batch_size=batch_size)

    return burn_in_predictions, unrolled_predictions, burned_in_rnn_states_inputs


# Adapted from: https://github.com/google-research/seed_rl/blob/34fb2874d41241eb4d5a03344619fb4e34dd9be6/agents/r2d2/learner.py#L333
def compute_loss(states: torch.Tensor,
                 actions: torch.Tensor,
                 next_states: torch.Tensor,
                 rewards: torch.Tensor,
                 non_terminals: torch.Tensor,
                 goals: torch.Tensor,
                 model: torch.nn.Module,
                 target_model: torch.nn.Module,
                 gamma: float = 0.99,
                 weights_decay_lambda: float = 1.0,
                 weights_entropy_lambda: float = 0.1,
                 use_PER: bool = False,
                 PER_beta: float = 1.0,
                 importanceSamplingWeights: torch.Tensor = None,
                 HER_target_clamping: bool = False,
                 summary_writer: object = None,
                 iteration_count: int = 0,
                 rnn_states: Dict[str, Dict[str, List[torch.Tensor]]] = None,
                 next_rnn_states: Dict[str, Dict[str, List[torch.Tensor]]] = None,
                 kwargs:Optional[Dict]=None) -> torch.Tensor:
    '''
    :param states: Dimension: batch_size x unroll_length x state_size: States visited by the agent.
    :param actions: Dimension: batch_size x unroll_length x action_size. Actions which the agent
                    took at every state in :param states: with the same index.
    :param next_states: Dimension: batch_size x unroll_length x state_size: Next sequence of unrolled states visited by the agent.
    :param non_terminals: Dimension: batch_size x unroll_length x 1: Non-terminal integers.
    :param rewards: Dimension: batch_size x unroll_length x 1. Environment rewards, or n-step returns if using n-step returns.
    :param goals: Dimension: batch_size x unroll_length x goal shape: Goal of the agent.
    :param model: torch.nn.Module used to compute the loss.
    :param target_model: torch.nn.Module used to compute the loss.
    :param gamma: float discount factor, or raised to the power of n if using n-step returns.
    :param weights_decay_lambda: Coefficient to be used for the weight decay loss.
    :param rnn_states: The :param model: can be made up of different submodules.
                       Some of these submodules will feature an LSTM architecture.
                       This parameter is a dictionary which maps recurrent submodule names
                       to a dictionary which contains 2 lists of tensors, each list
                       corresponding to the 'hidden' and 'cell' states of
                       the LSTM submodules. These tensors are used by the
                       :param model: when calculating the policy probability ratio.
    :param next_rnn_states: Resulting 'hidden' and 'cell' states of the LSTM submodules after
                            feedforwarding :param states: in :param model:. See :param rnn_states:
                            for further details on type and shape.
    '''
    batch_size = states.shape[0]
    unroll_length = states.shape[1]

    if kwargs['burn_in']:
        burn_in_length = kwargs['sequence_replay_burn_in_length']
        training_length = kwargs['sequence_replay_unroll_length']-burn_in_length

        burn_in_states, training_states = torch.split(
            states, 
            split_size_or_sections=[burn_in_length, training_length],
            dim=1
        )
        training_rnn_states = extract_rnn_states_from_time_indices(
            rnn_states, 
            time_indices_start=kwargs['sequence_replay_burn_in_length'],
            time_indices_end=kwargs['sequence_replay_unroll_length'],
        )
        _, training_rewards = torch.split(
            rewards, 
            split_size_or_sections=[burn_in_length, training_length],
            dim=1
        )
        _, training_non_terminals = torch.split(
            non_terminals, 
            split_size_or_sections=[burn_in_length, training_length],
            dim=1
        )

        burned_in_predictions, \
        unrolled_predictions, \
        burned_in_rnn_states_inputs = unrolled_inferences(
            model=model, 
            states=burn_in_states, 
            rnn_states=rnn_states,
            grad_enabler=False,
            use_zero_initial_states=kwargs['sequence_replay_use_zero_initial_states'],
            extras=False
        )

        target_model.reset_noise()

        burned_in_target_predictions, \
        unrolled_target_predictions, \
        burned_in_rnn_states_target_inputs = unrolled_inferences(
            model=target_model, 
            states=burn_in_states, 
            rnn_states=rnn_states,
            grad_enabler=False,
            use_zero_initial_states=kwargs['sequence_replay_use_zero_initial_states'],
            extras=False
        )

        # Replace the bruned in rnn states in the training rnn states:
        training_rnn_states = replace_rnn_states_at_time_indices(
            rnn_states_batched=training_rnn_states, 
            replacing_rnn_states_batched=burned_in_rnn_states_inputs, 
            time_indices_start=0, 
            time_indices_end=0
        )

        training_target_rnn_states = replace_rnn_states_at_time_indices(
            rnn_states_batched=training_rnn_states, 
            replacing_rnn_states_batched=burned_in_rnn_states_target_inputs, 
            time_indices_start=0, 
            time_indices_end=0
        ) 
    else:
        training_length = unroll_length
        training_states = states 
        training_rewards = rewards
        training_non_terminals = non_terminals
        training_rnn_states = rnn_states
        training_target_rnn_states = rnn_states

    training_next_states = next_states

    training_burned_in_predictions, \
    training_unrolled_predictions, _ = unrolled_inferences(
        model=model, 
        states=training_states, 
        rnn_states=training_rnn_states,
        grad_enabler=True,
        use_zero_initial_states=False,
        extras=True
    )

    target_model.reset_noise()

    training_burned_in_target_predictions, \
    training_unrolled_target_predictions, _ = unrolled_inferences(
        model=target_model, 
        states=training_states, 
        rnn_states=training_target_rnn_states,
        grad_enabler=False,
        use_zero_initial_states=False,
        extras=False
    )

    if kwargs['burn_in']:
        training_predictions = training_burned_in_predictions
        training_target_predictions = training_burned_in_target_predictions
    else:
        training_predictions = training_unrolled_predictions
        training_target_predictions = training_unrolled_target_predictions
    
    state_action_values = training_predictions["qa"]
    # (batch_size, unroll_dim, ...)
    current_actions = training_predictions["a"].reshape(batch_size, training_length, -1)
    # (batch_size, unroll_dim, ...)
    
    state_action_values_g = state_action_values.gather(dim=-1, index=current_actions).reshape(batch_size, training_length, -1)
    # (batch_size, unroll_dim, ...)
    
    targetQ_Si_Ai_values = training_target_predictions['qa']
    # (batch_size, training_length, ...)
    targetQ_Sipn_Aipn_values = torch.cat(
        [
            targetQ_Si_Ai_values[:, kwargs['n_step']:, ...]
        ]+[
            targetQ_Si_Ai_values[:, -1:, ...]/gamma # it will normalized down below when computing bellman target    
        ]*kwargs['n_step'],
        dim=1,
    )
    # (batch_size, training_length, ...)
    
    current_actions_ipn = torch.cat(
        [
            current_actions[:, kwargs['n_step']:, ...]
        ]+[
            current_actions[:, -1:, ...]    
        ]*kwargs['n_step'],
        dim=1,
    ).reshape(batch_size, training_length, -1)
    # (batch_size, training_length, 1)
    

    targetQ_Sipn_argmaxAQvalue = targetQ_Sipn_Aipn_values.gather(dim=-1, index=current_actions_ipn).reshape(batch_size, training_length, -1)
    # (batch_size, training_length, -1)

    # Compute the Bellman Target for Q values at Si,Ai: with gamma <-- gamma ** n_step ...
    unscaled_targetQ_Sipn_argmaxAQvalue = inverse_value_function_rescaling(targetQ_Sipn_argmaxAQvalue)
    bellman_target_Sipn_Aipn = training_rewards + (gamma * unscaled_targetQ_Sipn_argmaxAQvalue)*training_non_terminals
    # (batch_size, training_length, ...)
    scaled_bellman_target_Sipn_Aipn = value_function_rescaling(bellman_target_Sipn_Aipn)

    '''
    # TODO: decide how to handle HER augmentation...
    if HER_target_clamping:
        # clip the target to [-50,0]
        expected_state_action_values = torch.clamp(expected_state_action_values, -1. / (1 - gamma), 0)
    '''

    # Compute loss:
    state_action_values_g = state_action_values_g.reshape(scaled_bellman_target_Sipn_Aipn.shape)

    td_error = torch.abs(scaled_bellman_target_Sipn_Aipn - state_action_values_g)
    loss_per_item = td_error
    diff_squared = td_error.pow(2.0)

    if use_PER:
      diff_squared = importanceSamplingWeights * diff_squared

    loss = 0.5*torch.mean(diff_squared)-weights_entropy_lambda*training_predictions['ent'].mean()

    if summary_writer is not None:
        denominator = eps+torch.abs(training_burned_in_predictions['qa'].reshape(batch_size, -1).max(dim=-1)[0])
        # (batch_size, )
        initial_diff = training_burned_in_predictions['qa'][:,0,...]-training_unrolled_predictions['qa'][:,0,...]
        # (batch_size, num_actions)
        final_diff = training_burned_in_predictions['qa'][:,-1,...]-training_unrolled_predictions['qa'][:,-1,...]
        # (batch_size, num_actions)
        initial_discrepancy_qa = initial_diff.pow(2).sum(-1).sqrt() / denominator
        # (batch_size,)
        final_discrepancy_qa = final_diff.pow(2).sum(-1).sqrt() / denominator
        # (batch_size, )
        
        summary_writer.add_scalar('Training/DiscrepancyQAValues/Initial', initial_discrepancy_qa.cpu().mean().item(), iteration_count)
        summary_writer.add_scalar('Training/DiscrepancyQAValues/Final', final_discrepancy_qa.cpu().mean().item(), iteration_count)
        
        summary_writer.add_scalar('Training/MeanQAValues', training_predictions['qa'].cpu().mean().item(), iteration_count)
        summary_writer.add_scalar('Training/StdQAValues', training_predictions['qa'].cpu().std().item(), iteration_count)
        summary_writer.add_scalar('Training/QAValueLoss', loss.cpu().item(), iteration_count)
        summary_writer.add_scalar('Training/EntropyVal', training_predictions['ent'].mean().cpu().item(), iteration_count)
        #summary_writer.add_scalar('Training/TotalLoss', loss.cpu().item(), iteration_count)
        if use_PER:
            summary_writer.add_scalar('Training/ImportanceSamplingMean', importanceSamplingWeights.cpu().mean().item(), iteration_count)
            summary_writer.add_scalar('Training/ImportanceSamplingStd', importanceSamplingWeights.cpu().std().item(), iteration_count)
            summary_writer.add_scalar('Training/PER_Beta', PER_beta, iteration_count)

    return loss, loss_per_item