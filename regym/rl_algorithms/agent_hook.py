import torch
from .agents import TabularQLearningAgent, DeepQNetworkAgent, PPOAgent, MixedStrategyAgent
from enum import Enum

AgentType = Enum("AgentType", "DQN TQL PPO MixedStrategyAgent")


class AgentHook():
    def __init__(self, agent, save_path=None):
        """
        Creates an agent hook which allows to transport :param: agent:
        - Between processes if by making all Torch.Tensors be in CPU IF :param: save_path is None
        - Written to disk if at path :param: save_path if it is not None

        :param agent: Agent to be hooked to be transported between processes
        :param save_path: path where to save the current agent.
        :returns: AgentHook agent whose type is that of :param: agent
        """

        self.name = agent.name
        self.save_path = save_path

        if isinstance(agent, MixedStrategyAgent):
            agent_type, model_list = AgentType.MixedStrategyAgent, []
        elif isinstance(agent, TabularQLearningAgent):
            agent_type, model_list = AgentType.TQL, []
        elif isinstance(agent, DeepQNetworkAgent):
            agent_type, model_list = AgentType.DQN, [('model', agent.algorithm.model), ('target_model', agent.algorithm.target_model)]
        elif isinstance(agent, PPOAgent):
            agent_type, model_list = AgentType.PPO, [('model', agent.algorithm.model)]
        self.hook_agent(agent, agent_type, model_list)

    def hook_agent(self, agent, agent_type, model_list):
        self.type, self.model_list = agent_type, model_list
        for _, model in model_list: model.cpu()
        if not self.save_path: self.agent = agent
        else: torch.save(agent, self.save_path)

    @staticmethod
    def unhook(agent_hook, use_cuda=None, nbr_actor=None):
        if hasattr(agent_hook, 'save_path') and agent_hook.save_path is not None: agent_hook.agent = torch.load(agent_hook.save_path)
        if agent_hook.type == AgentType.TQL or agent_hook.type == AgentType.MixedStrategyAgent: return agent_hook.agent
        if 'use_cuda' in agent_hook.agent.algorithm.kwargs:
            if use_cuda is not None:
               agent_hook.agent.algorithm.kwargs['use_cuda'] = use_cuda
               if hasattr(agent_hook.agent, 'state_preprocessing'): agent_hook.agent.state_preprocessing.use_cuda = use_cuda
            if agent_hook.agent.algorithm.kwargs['use_cuda']:
                for name, model in agent_hook.model_list: setattr(agent_hook.agent.algorithm, name, model.cuda())
        if nbr_actor is not None :
            agent_hook.agent.set_nbr_actor(nbr_actor)
        return agent_hook.agent

    @staticmethod
    def load(load_path):
        agent = torch.load(load_path)
        hook = AgentHook(agent)
        if 'use_cuda' in agent.algorithm.kwargs and agent.algorithm.kwargs['use_cuda']:
            for name, model in hook.model_list: setattr(agent.algorithm, name, model.cuda())
        return agent
