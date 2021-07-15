from typing import Dict, Any
import torch
import numpy as np
from copy import deepcopy
import random
from collections.abc import Iterable

from ..algorithms.DQN import DQNAlgorithm, dqn_loss, ddqn_loss
from ..networks import PreprocessFunction, ResizeCNNPreprocessFunction, ResizeCNNInterpolationFunction
from regym.rl_algorithms.agents.utils import generate_model, parse_and_check 

import ray
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from functools import partial

from .agent import Agent
#from .wrappers import DictHandlingAgentWrapper
from regym.rl_algorithms.agents.agent import ExtraInputsHandlingAgent
from gym.spaces import Dict as gymDict
#from ..algorithms.wrappers import HERAlgorithmWrapper
from regym.rl_algorithms.algorithms.wrappers import HERAlgorithmWrapper2

from regym.rl_algorithms.utils import _extract_from_rnn_states, copy_hdict
from regym.rl_algorithms.utils import apply_on_hdict, _concatenate_list_hdict


#class DQNHERAgent(Agent):
class DQNHERAgent(ExtraInputsHandlingAgent):
    def __init__(self, name, algorithm, extra_inputs_infos):
        super(DQNHERAgent, self).__init__(
            name=name, 
            algorithm=algorithm,
            extra_inputs_infos=extra_inputs_infos
        )
        
        self.kwargs = algorithm.kwargs
        self.epsend = float(self.kwargs['epsend'])
        self.epsstart = float(self.kwargs['epsstart'])
        self.epsdecay = float(self.kwargs['epsdecay'])
        self.epsdecay_strategy = self.kwargs['epsdecay_strategy'] if 'epsdecay_strategy' in self.kwargs else 'exponential'
        self.eps = None

        self.replay_period = int(self.kwargs['replay_period']) if 'replay_period' in self.kwargs else 1
        self.replay_period_count = 0

        self.nbr_episode_per_cycle = int(self.kwargs['nbr_episode_per_cycle']) if 'nbr_episode_per_cycle' in self.kwargs else None
        self.nbr_episode_per_cycle_count = 0

        self.nbr_training_iteration_per_cycle = int(self.kwargs['nbr_training_iteration_per_cycle']) if 'nbr_training_iteration_per_cycle' in self.kwargs else 1

        self.noisy = self.kwargs['noisy'] if 'noisy' in self.kwargs else False

        # Number of interaction/step with/in the environment:
        self.nbr_steps = 0

        self.saving_interval = float(self.kwargs['saving_interval']) if 'saving_interval' in self.kwargs else 1e5
        
        self.previous_save_quotient = -1

    def get_update_count(self):
        return self.algorithm.unwrapped.get_update_count()

    #def handle_experience(self, s, a, r, succ_s, done, goals=None, infos=None, prediction=None):
    def _handle_experience(self, s, a, r, succ_s, done, goals=None, infos=None, prediction=None):
        '''
        Note: the batch size may differ from the nbr_actor as soon as some
        actors' episodes end before the others...

        :param s: numpy tensor of states of shape batch x state_shape.
        :param a: numpy tensor of actions of shape batch x action_shape.
        :param r: numpy tensor of rewards of shape batch x reward_shape.
        :param succ_s: numpy tensor of successive states of shape batch x state_shape.
        :param done: list of boolean (batch=nbr_actor) x state_shape.
        :param goals: Dictionnary of goals 'achieved_goal' and 'desired_goal' for each state 's' and 'succ_s'.
        :param infos: Dictionnary of information from the environment.
        :param prediction: Dictionnary of tensors containing the model's output at the current state.
        '''
        if "sad" in self.kwargs \
        and self.kwargs["sad"]:
            a = a["action"]

        if prediction is None:  prediction = deepcopy(self.current_prediction)

        state, r, succ_state, non_terminal = self.preprocess_environment_signals(s, r, succ_s, done)
        a = torch.from_numpy(a)
        # batch x ...

        batch_size = a.shape[0]

        if "vdn" in self.kwargs \
        and self.kwargs["vdn"]:
            # Add a player dimension to each element:
            # Assume inputs have shape : [batch_size*nbr_players, ...],
            # i.e. [batch_for_p0; batch_for_p1, ...]
            nbr_players = self.kwargs["vdn_nbr_players"]
            batch_size = state.shape[0] // nbr_players
            
            new_state = []
            for bidx in range(batch_size):
                bidx_states = torch.stack(
                    [
                        state[pidx*batch_size+bidx].unsqueeze(0) 
                        for pidx in range(nbr_players)
                    ], 
                    dim=1
                )
                new_state.append(bidx_states)
            state = torch.cat(new_state, dim=0)
            
            new_a = []
            for bidx in range(batch_size):
                bidx_as = torch.stack(
                    [
                        a[pidx*batch_size+bidx].unsqueeze(0) 
                        for pidx in range(nbr_players)
                    ], 
                    dim=1
                )
                new_a.append(bidx_as)
            a = torch.cat(new_a, dim=0)

            new_r = []
            for bidx in range(batch_size):
                bidx_rs = torch.stack(
                    [
                        r[pidx*batch_size+bidx].unsqueeze(0) 
                        for pidx in range(nbr_players)
                    ], 
                    dim=1
                )
                new_r.append(bidx_rs)
            r = torch.cat(new_r, dim=0)

            '''
            non_terminal = torch.cat([non_terminal]*2, dim=0)
            new_nt = []
            for bidx in range(batch_size):
                bidx_nts = torch.stack([non_terminal[pidx*batch_size+bidx].unsqueeze(0) for pidx in range(nbr_players)], dim=1)
                new_nt.append(bidx_nts)
            non_terminal = torch.cat(new_nt, dim=0)            
            '''
            
            new_succ_state = []
            for bidx in range(batch_size):
                bidx_succ_states = torch.stack(
                    [
                        succ_state[pidx*batch_size+bidx].unsqueeze(0) 
                        for pidx in range(nbr_players)
                    ], 
                    dim=1
                )
                new_succ_state.append(bidx_succ_states)
            succ_state = torch.cat(new_succ_state, dim=0)
            
            # BEWARE: reshaping might not give the expected ordering due to the dimensions' ordering...
            #hdict_reshape_fn = lambda x: x.reshape(batch_size, nbr_players, *x.shape[1:])
            # The above fails to capture the correct ordering:
            # [ batch0=[p0_exp1, p0_exp2 ; .. ]] instead of 
            # [ batch0=[p0_exp1, p1_exp1 ; .. ]], if only two players are considered...  
            def reshape_fn(x):
                new_x = []
                for bidx in range(batch_size):
                    bidx_x = torch.stack(
                        [
                            x[pidx*batch_size+bidx].unsqueeze(0) 
                            for pidx in range(nbr_players)
                        ], 
                        dim=1
                    )
                    new_x.append(bidx_x)
                return torch.cat(new_x, dim=0)
                
            for k, t in prediction.items():
                if isinstance(t, torch.Tensor):
                    #prediction[k] = t.reshape(batch_size, nbr_players, *t.shape[1:])
                    prediction[k] = reshape_fn(prediction[k])
                elif isinstance(t, dict):
                    prediction[k] = apply_on_hdict(
                        hdict=t,
                        fn=reshape_fn, #hdict_reshape_fn,
                    )
                else:
                    raise NotImplementedError
            
            """
            # not used...
            # Infos: list of batch_size * nbr_players dictionnaries:
            new_infos = []
            for bidx in range(batch_size):
                bidx_infos = [infos[pidx*batch_size+bidx] for pidx in range(nbr_players)]
                bidx_info = _concatenate_list_hdict(
                    lhds=bidx_infos,
                    concat_fn=partial(np.stack, axis=1),   #new player dimension
                    preprocess_fn=(lambda x: x),
                )
                new_infos.append(bidx_info)
            infos = new_infos
            
            # Goals:
            if self.goal_oriented:
                raise NotImplementedError
            """

        # We assume that this function has been called directly after take_action:
        # therefore the current prediction correspond to this experience.

        batch_index = -1
        done_actors_among_notdone = []
        #for actor_index in range(self.nbr_actor):
        for actor_index in range(batch_size):
            # If this actor is already done with its episode:
            if self.previously_done_actors[actor_index]:
                continue
            # Otherwise, there is bookkeeping to do:
            batch_index +=1

            # Bookkeeping of the actors whose episode just ended:
            if done[actor_index] and not(self.previously_done_actors[actor_index]):
                done_actors_among_notdone.append(batch_index)

            exp_dict = {}
            exp_dict['s'] = state[batch_index,...].unsqueeze(0)
            exp_dict['a'] = a[batch_index,...].unsqueeze(0)
            exp_dict['r'] = r[batch_index,...].unsqueeze(0)
            exp_dict['succ_s'] = succ_state[batch_index,...].unsqueeze(0)
            # Watch out for the miss-match:
            # done is a list of nbr_actor booleans,
            # which is not sync with batch_index, purposefully...
            exp_dict['non_terminal'] = non_terminal[actor_index,...].unsqueeze(0)
            # Watch out for the miss-match:
            # Similarly, infos is not sync with batch_index, purposefully...
            if infos is not None:
                exp_dict['info'] = infos[actor_index]

            #########################################################################
            #########################################################################
            # Exctracts tensors at root level:
            exp_dict.update(Agent._extract_from_prediction(prediction, batch_index))
            #########################################################################
            #########################################################################
            

            # Extracts remaining info:
            if self.recurrent:
                exp_dict['rnn_states'] = _extract_from_rnn_states(
                    prediction['rnn_states'],
                    batch_index,
                    post_process_fn=(lambda x: x.detach().cpu())
                )
                exp_dict['next_rnn_states'] = _extract_from_rnn_states(
                    prediction['next_rnn_states'],
                    batch_index,
                    post_process_fn=(lambda x: x.detach().cpu())
                )

            """
            # depr : goal update
            if self.goal_oriented:
                exp_dict['goals'] = Agent._extract_from_hdict(
                    goals, 
                    batch_index, 
                    goal_preprocessing_fn=self.goal_preprocessing
                )
            """

            self.algorithm.store(exp_dict, actor_index=actor_index)
            self.previously_done_actors[actor_index] = done[actor_index]
            self.handled_experiences +=1

        self.replay_period_count += 1
        if self.nbr_episode_per_cycle is not None:
            if len(done_actors_among_notdone):
                self.nbr_episode_per_cycle_count += len(done_actors_among_notdone)
        
        if not(self.async_actor):
            self.train()

    def train(self):
        nbr_updates = 0

        period_check = self.replay_period
        period_count_check = self.replay_period_count
        if self.nbr_episode_per_cycle is not None:
            period_check = self.nbr_episode_per_cycle
            period_count_check = self.nbr_episode_per_cycle_count

        if self.training \
        and self.handled_experiences > self.kwargs['min_capacity'] \
        and self.algorithm.unwrapped.stored_experiences() > self.kwargs['min_capacity'] \
        and (period_count_check % period_check == 0 or not(self.async_actor)):
            minibatch_size = self.kwargs['batch_size']
            if self.nbr_episode_per_cycle is None:
                minibatch_size *= self.replay_period
            else:
                self.nbr_episode_per_cycle_count = 1

            for train_it in range(self.nbr_training_iteration_per_cycle):
                self.algorithm.train(minibatch_size=minibatch_size)
            
            nbr_updates = self.nbr_training_iteration_per_cycle

            if self.algorithm.unwrapped.summary_writer is not None:
                if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                    actor_learner_shared_dict = ray.get(self.actor_learner_shared_dict.get.remote())
                else:
                    actor_learner_shared_dict = self.actor_learner_shared_dict.get()
                nbr_update_remaining = sum(actor_learner_shared_dict["models_update_required"])
                self.algorithm.unwrapped.summary_writer.add_scalar(
                    f'PerUpdate/ActorLearnerSynchroRemainingUpdates', 
                    nbr_update_remaining, 
                    self.algorithm.unwrapped.get_update_count()
                )
            
            # Update actor's models:
            if self.async_learner\
            and (self.handled_experiences // self.actor_models_update_steps_interval) != self.previous_actor_models_update_quotient:
                self.previous_actor_models_update_quotient = self.handled_experiences // self.actor_models_update_steps_interval
                new_models_cpu = {k:deepcopy(m).cpu() for k,m in self.algorithm.unwrapped.get_models().items()}
                
                if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                    actor_learner_shared_dict = ray.get(self.actor_learner_shared_dict.get.remote())
                else:
                    actor_learner_shared_dict = self.actor_learner_shared_dict.get()
                
                actor_learner_shared_dict["models"] = new_models_cpu
                actor_learner_shared_dict["models_update_required"] = [True]*len(actor_learner_shared_dict["models_update_required"])
                
                if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                    self.actor_learner_shared_dict.set.remote(actor_learner_shared_dict)
                else:
                    self.actor_learner_shared_dict.set(actor_learner_shared_dict)

            if self.async_learner\
            and self.save_path is not None \
            and (self.algorithm.unwrapped.get_update_count() // self.saving_interval) != self.previous_save_quotient:
                self.previous_save_quotient = self.algorithm.unwrapped.get_update_count() // self.saving_interval
                self.save()

        return nbr_updates

    #def take_action(self, state, infos=None, as_logit=False):
    def _take_action(self, state, infos=None, as_logit=False):
        if self.async_actor:
            # Update the algorithm's model if needs be:
            if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                actor_learner_shared_dict = ray.get(self.actor_learner_shared_dict.get.remote())
            else:
                actor_learner_shared_dict = self.actor_learner_shared_dict.get()
            if actor_learner_shared_dict["models_update_required"][self.async_actor_idx]:
                actor_learner_shared_dict["models_update_required"][self.async_actor_idx] = False
                
                if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                    self.actor_learner_shared_dict.set.remote(actor_learner_shared_dict)
                else:
                    self.actor_learner_shared_dict.set(actor_learner_shared_dict)
                
                if "models" in actor_learner_shared_dict.keys():
                    new_models = actor_learner_shared_dict["models"]
                    self.algorithm.unwrapped.set_models(new_models)
                else:
                    raise NotImplementedError 

        if self.training:
            self.nbr_steps += state.shape[0]
        self.eps = self.algorithm.unwrapped.get_epsilon(nbr_steps=self.nbr_steps, strategy=self.epsdecay_strategy)
        if "vdn" in self.kwargs \
        and self.kwargs["vdn"]:
            # The following will not make same values contiguous:
            #self.eps = np.concatenate([self.eps]*self.kwargs["vdn_nbr_players"], axis=0)
            # whereas the following will, and thus players in the same environment will explore similarly:
            self.eps = np.stack([self.eps]*self.kwargs["vdn_nbr_players"], axis=-1).reshape(-1)


        state = self.state_preprocessing(state, use_cuda=self.algorithm.unwrapped.kwargs['use_cuda'])
        
        """
        # depr : goal update
        goal = None
        if self.goal_oriented:
            goal = self.goal_preprocessing(self.goals, use_cuda=self.algorithm.unwrapped.kwargs['use_cuda'])
        """

        model = self.algorithm.unwrapped.get_models()['model']
        if 'use_target_to_gather_data' in self.kwargs and self.kwargs['use_target_to_gather_data']:
            model = self.algorithm.unwrapped.get_models()['target_model']
        model = model.train(mode=self.training)

        
        # depr : goal update
        #self.current_prediction = self.query_model(model, state, goal)
        self.current_prediction = self.query_model(model, state)
        
        if as_logit:
            return self.current_prediction['log_a']

        # Post-process and update the rnn_states from the current prediction:
        # self.rnn_states <-- self.current_prediction['next_rnn_states']
        # WARNING: _post_process affects self.rnn_states. It is imperative to
        # manipulate a copy of it outside of the agent's manipulation, e.g.
        # when feeding it to the models.
        self.current_prediction = self._post_process(self.current_prediction)

        greedy_action = self.current_prediction['a'].reshape((-1,1)).numpy()

        if self.noisy or not(self.training):
            return greedy_action

        legal_actions = torch.ones_like(self.current_prediction['qa'])
        if infos is not None\
        and 'head' in infos\
        and 'extra_inputs' in infos['head']\
        and 'legal_actions' in infos['head']['extra_inputs']:
            legal_actions = infos['head']['extra_inputs']['legal_actions'][0]
            # in case there are no legal actions for this agent in this current turn:
            for actor_idx in range(legal_actions.shape[0]):
                if legal_actions[actor_idx].sum() == 0: 
                    legal_actions[actor_idx, ...] = 1
        sample = np.random.random(size=self.eps.shape)
        greedy = (sample > self.eps)
        greedy = np.reshape(greedy[:state.shape[0]], (state.shape[0],1))

        #random_actions = [random.randrange(model.action_dim) for _ in range(state.shape[0])]
        random_actions = [
            legal_actions[actor_idx].multinomial(num_samples=1).item() 
            for actor_idx in range(legal_actions.shape[0])
        ]
        random_actions = np.reshape(np.array(random_actions), (state.shape[0],1))
        
        actions = greedy*greedy_action + (1-greedy)*random_actions
        
        if "sad" in self.kwargs \
        and self.kwargs["sad"]:
            action_dict = {
                'action': actions,
                'greedy_action': greedy_action,
            }
            return action_dict 

        return actions

    #def query_action(self, state, infos=None, as_logit=False):
    def _query_action(self, state, infos=None, as_logit=False):
        """
        Query's the model in training mode...
        """
        if self.async_actor:
            # Update the algorithm's model if needs be:
            if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                actor_learner_shared_dict = ray.get(self.actor_learner_shared_dict.get.remote())
            else:
                actor_learner_shared_dict = self.actor_learner_shared_dict.get()
            if actor_learner_shared_dict["models_update_required"][self.async_actor_idx]:
                actor_learner_shared_dict["models_update_required"][self.async_actor_idx] = False
                
                if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
                    self.actor_learner_shared_dict.set.remote(actor_learner_shared_dict)
                else:
                    self.actor_learner_shared_dict.set(actor_learner_shared_dict)
                
                if "models" in actor_learner_shared_dict.keys():
                    new_models = actor_learner_shared_dict["models"]
                    self.algorithm.unwrapped.set_models(new_models)
                else:
                    raise NotImplementedError 

        self.eps = self.algorithm.unwrapped.get_epsilon(nbr_steps=self.nbr_steps, strategy=self.epsdecay_strategy)
        if "vdn" in self.kwargs \
        and self.kwargs["vdn"]:
            # The following will not make same values contiguous:
            #self.eps = np.concatenate([self.eps]*self.kwargs["vdn_nbr_players"], axis=0)
            # whereas the following will, and thus players in the same environment will explore similarly:
            self.eps = np.stack([self.eps]*self.kwargs["vdn_nbr_players"], axis=-1).reshape(-1)


        state = self.state_preprocessing(state, use_cuda=self.algorithm.unwrapped.kwargs['use_cuda'])
        
        """
        # depr : goal update
        goal = None
        if self.goal_oriented:
            goal = self.goal_preprocessing(self.goals, use_cuda=self.algorithm.unwrapped.kwargs['use_cuda'])
        """

        model = self.algorithm.unwrapped.get_models()['model']
        if 'use_target_to_gather_data' in self.kwargs and self.kwargs['use_target_to_gather_data']:
            model = self.algorithm.unwrapped.get_models()['target_model']
        if not(model.training):  model = model.train(mode=True)

        # depr : goal update
        #current_prediction = self.query_model(model, state, goal)
        current_prediction = self.query_model(model, state)
        
        if as_logit:
            return current_prediction['log_a']

        # Post-process and update the rnn_states from the current prediction:
        # self.rnn_states <-- self.current_prediction['next_rnn_states']
        # WARNING: _post_process affects self.rnn_states. It is imperative to
        # manipulate a copy of it outside of the agent's manipulation, e.g.
        # when feeding it to the models.
        current_prediction = self._post_process(current_prediction)

        greedy_action = current_prediction['a'].reshape((-1,1)).numpy()

        if self.noisy:
            return greedy_action

        legal_actions = torch.ones_like(current_prediction['qa'])
        if infos is not None\
        and 'head' in infos\
        and 'extra_inputs' in infos['head']\
        and 'legal_actions' in infos['head']['extra_inputs']:
            legal_actions = infos['head']['extra_inputs']['legal_actions'][0]
            # in case there are no legal actions for this agent in this current turn:
            for actor_idx in range(legal_actions.shape[0]):
                if legal_actions[actor_idx].sum() == 0: 
                    legal_actions[actor_idx, ...] = 1
        sample = np.random.random(size=self.eps.shape)
        greedy = (sample > self.eps)
        greedy = np.reshape(greedy[:state.shape[0]], (state.shape[0],1))

        #random_actions = [random.randrange(model.action_dim) for _ in range(state.shape[0])]
        random_actions = [
            legal_actions[actor_idx].multinomial(num_samples=1).item() 
            for actor_idx in range(legal_actions.shape[0])
        ]
        random_actions = np.reshape(np.array(random_actions), (state.shape[0],1))
        
        actions = greedy*greedy_action + (1-greedy)*random_actions
        
        if "sad" in self.kwargs \
        and self.kwargs["sad"]:
            action_dict = {
                'action': actions,
                'greedy_action': greedy_action,
            }
            return action_dict 

        return actions

    def query_model(self, model, state, goal=None):
        if self.recurrent:
            self._pre_process_rnn_states()
            # WARNING: it is imperative to make a copy 
            # of the self.rnn_states, otherwise it will be 
            # referenced in the (self.)current_prediction
            # and any subsequent update of rnn_states will 
            # also update the current_prediction, e.g. the call
            # to _post_process in line 163 affects self.rnn_states
            # and therefore might affect current_prediction's rnn_states...
            rnn_states_input = copy_hdict(self.rnn_states)
            current_prediction = model(state, rnn_states=rnn_states_input, goal=goal)
        else:
            current_prediction = model(state, goal=goal)
        return current_prediction

    def clone(self, training=None, with_replay_buffer=False, clone_proxies=False, minimal=False):
        cloned_algo = self.algorithm.clone(
            with_replay_buffer=with_replay_buffer,
            clone_proxies=clone_proxies,
            minimal=minimal
        )
        clone = DQNHERAgent(
            name=self.name, 
            algorithm=cloned_algo,
            extra_inputs_infos=self.extra_inputs_infos
        )
        clone.save_path = self.save_path
        
        clone.actor_learner_shared_dict = self.actor_learner_shared_dict
        clone._handled_experiences = self._handled_experiences
        clone.episode_count = self.episode_count
        if training is not None:    clone.training = training
        clone.nbr_steps = self.nbr_steps

        # Goes through all variables 'Proxy' (dealing with multiprocessing)
        # contained in this class and removes them from clone
        if not(clone_proxies):
            proxy_key_values = [
                (key, value) 
                for key, value in clone.__dict__.items() 
                if ('Proxy' in str(type(value)))
            ]
            for key, value in proxy_key_values:
                setattr(clone, key, None)

        return clone

    def get_async_actor(self, training=None, with_replay_buffer=False):
        self.async_learner = True
        self.async_actor = False 

        cloned_algo = self.algorithm.async_actor()
        clone = DQNHERAgent(
            name=self.name, 
            algorithm=cloned_algo,
            extra_inputs_infos=self.extra_inputs_infos
        )
        
        clone.async_learner = False 
        clone.async_actor = True 

        ######################################
        ######################################
        # Update actor_learner_shared_dict:
        ######################################
        if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
            actor_learner_shared_dict = ray.get(self.actor_learner_shared_dict.get.remote())
        else:
            actor_learner_shared_dict = self.actor_learner_shared_dict.get()
        # Increase the size of the list of toggle booleans:
        actor_learner_shared_dict["models_update_required"] += [False]
        
        # Update the (Ray)SharedVariable            
        if isinstance(self.actor_learner_shared_dict, ray.actor.ActorHandle):
            self.actor_learner_shared_dict.set.remote(actor_learner_shared_dict)
        else:
            self.actor_learner_shared_dict.set(actor_learner_shared_dict)
        
        ######################################
        # Update the async_actor index:
        clone.async_actor_idx = len(actor_learner_shared_dict["models_update_required"])-1

        ######################################
        ######################################
        
        clone.actor_learner_shared_dict = self.actor_learner_shared_dict
        clone._handled_experiences = self._handled_experiences
        clone.episode_count = self.episode_count
        if training is not None:    clone.training = training
        clone.nbr_steps = self.nbr_steps
        return clone


def build_DQN_HER_Agent(task, config, agent_name):
    '''
    :param task: Environment specific configuration
    :param config: Dict containing configuration for ppo agent
    :param agent_name: name of the agent
    :returns: DeepQNetworkAgent adapted to be trained on :param: task under :param: config
    '''
    kwargs = config.copy()
    kwargs['discount'] = float(kwargs['discount'])
    kwargs['replay_capacity'] = int(float(kwargs['replay_capacity']))
    kwargs['min_capacity'] = int(float(kwargs['min_capacity']))

    # Default preprocess function:
    kwargs['state_preprocess'] = partial(PreprocessFunction, normalization=False)
    kwargs['goal_preprocess'] = partial(PreprocessFunction, normalization=False)

    #if not isinstance(kwargs['observation_resize_dim'], int):  kwargs['observation_resize_dim'] = task.observation_shape[0] if isinstance(task.observation_shape, tuple) else task.observation_shape
    if 'observation_resize_dim' in kwargs\
    and not isinstance(kwargs['observation_resize_dim'], int):  
        kwargs['observation_resize_dim'] = task.observation_shape[0] if isinstance(task.observation_shape, tuple) else task.observation_shape
    #if 'None' in kwargs['goal_resize_dim']:  kwargs['goal_resize_dim'] = task.goal_shape[0] if isinstance(task.goal_shape, tuple) else task.goal_shape

    kwargs = parse_and_check(kwargs, task)

    model = generate_model(task, kwargs)

    loss_fn = dqn_loss.compute_loss
    if kwargs['double'] or kwargs['dueling']:
        loss_fn = ddqn_loss.compute_loss

    dqn_algorithm = DQNAlgorithm(kwargs, model, loss_fn=loss_fn)

    """
    if 'use_HER' in kwargs and kwargs['use_HER']:
        from ..algorithms.wrappers import latent_based_goal_predicated_reward_fn
        goal_predicated_reward_fn = None
        if 'HER_use_latent' in kwargs and kwargs['HER_use_latent']:
            goal_predicated_reward_fn = latent_based_goal_predicated_reward_fn

        dqn_algorithm = HERAlgorithmWrapper(algorithm=dqn_algorithm,
                                            strategy=kwargs['HER_strategy'],
                                            goal_predicated_reward_fn=goal_predicated_reward_fn)
    """

    if kwargs.get('use_HER', False):
        from regym.rl_algorithms.algorithms.wrappers import latent_based_goal_predicated_reward_fn2
        goal_predicated_reward_fn = None
        if kwargs.get('HER_use_latent', False):
            goal_predicated_reward_fn = latent_based_goal_predicated_reward_fn2

        dqn_algorithm = HERAlgorithmWrapper2(
            algorithm=dqn_algorithm,
            strategy=kwargs['HER_strategy'],
            goal_predicated_reward_fn=goal_predicated_reward_fn,
            extra_inputs_infos=kwargs['extra_inputs_infos'],
        )

    agent = DQNHERAgent(
        name=agent_name, 
        algorithm=dqn_algorithm,
        extra_inputs_infos=kwargs['extra_inputs_infos'],
    )

    """
    if isinstance(getattr(task.env, 'observation_space', None), gymDict) or ('use_HER' in kwargs and kwargs['use_HER']):
        agent = DictHandlingAgentWrapper(agent=agent, use_achieved_goal=kwargs['use_HER'])
    """

    print(dqn_algorithm.get_models())

    return agent