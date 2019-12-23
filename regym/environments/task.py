from enum import Enum
from typing import Callable, Any
import gym


class EnvType(Enum):
    SINGLE_AGENT = 'single-agent'
    MULTIAGENT_SIMULTANEOUS_ACTION = 'multiagent-simultaneous'
    MULTIAGENT_SEQUENTIAL_ACTION = 'multiagent-sequential'


class Task:
    r'''
    A Task is a thin layer of abstraction over OpenAI gym environments and
    Unity ML-agents executables, used across Regym.
    The main uses of Tasks are: 
        - Initialize agents capable of acting in an environment via the
          `build_X_Agent()` functions where `X` is an algorithm from
          `regym.rl_algorithms`.
        - Run episodes of the underlying environment via the `task.run_episode` function.

    NOTE: Unless you know what you are doing, a Task should be generated thus:
    >>> from regym.environments import generate_task
    >>> task = generate_task('OpenAIGymEnv-v0')

    Tasks can encapsulate 3 types of environments. Captured in the class
    `regym.environments.EnvType`:
        - SINGLE_AGENT
        - MULTIAGENT_SIMULTANEOUS_ACTION
        - MULTIAGENT_SEQUENTIAL_ACTION

    Single agent environments are self-explanatory. In sequential action
    environments, the environment will process a single agent action on every
    `env.step` function call. Simultaenous action environments will take an
    action from every player on each `env.step` function call.

    For multiagent environments, it is mandatory to specify whether the
    actions are consumed simultaneously or sequentially by the environment.
    This is done via passing an EnvType to the `generate_task` function.

    >>> from regym.environments import EnvType
    >>> simultaneous_task = generate_task('SimultaneousEnv-v0', EnvType.MULTIAGENT_SIMULTANEOUS_ACTION)
    >>> sequential_task   = generate_task('SequentialsEnv-v0',  EnvType.MULTIAGENT_SEQUENTIAL_ACTION)
    '''

    def __init__(self, name: str,
                 env: gym.Env,
                 env_type: EnvType,
                 test_env: gym.Env,
                 state_space_size: int,
                 action_space_size: int,
                 observation_shape: int,
                 observation_type: str,
                 action_dim: int,
                 action_type: str,
                 hash_function: Callable[[Any], int]):
        '''
        TODO Document
        '''
        self.name = name
        self.env = env
        self.env_type = env_type
        self.test_env = test_env
        self.state_space_size = state_space_size
        self.action_space_size = action_space_size
        self.observation_shape = observation_shape
        self.observation_type = observation_type
        self.action_dim = action_dim
        self.action_type = action_type
        self.hash_function = hash_function

        self.total_episodes_run = 0


    def run_episode(self, agent_vector, training):
        '''
        TODO Document
        '''
        self.total_episodes_run += 1
        if self.env_type == EnvType.SINGLE_AGENT:
            from regym.rl_loops.singleagent_loops import rl_loop
            if training:
                return rl_loop.run_episode_parallel(self.env, agent_vector, True)
            else:
                return rl_loop.run_episode_parallel(self.test_env, agent_vector, False)
        if self.env_type == EnvType.MULTIAGENT_SIMULTANEOUS_ACTION:
            from regym.rl_loops.multiagent_loops import simultaneous_action_rl_loop
            if training:
                return simultaneous_action_rl_loop.run_episode_parallel(self.env, agent_vector, True)
            else:
                return simultaneous_action_rl_loop.run_episode_parallel(self.test_env, agent_vector, False)
        if self.env_type == EnvType.MULTIAGENT_SEQUENTIAL_ACTION:
            from regym.rl_loops.multiagent_loops import sequential_action_rl_loop
            if training:
                return sequential_action_rl_loop.run_episode(self.env, agent_vector, True)
            else:
                return sequential_action_rl_loop.run_episode(self.test_env, agent_vector, False)

    def __repr__(self):
        s = \
f'''
Task: {self.name}
env: {self.env}
env_type: {self.env_type}
test_env: {self.test_env}
observation_shape: {self.observation_shape}
observation_type: {self.observation_type}
state_space_size: {self.state_space_size}
action_space_size: {self.action_space_size}
action_dim: {self.action_dim}
action_type: {self.action_type}
hash_function: {self.hash_function}
'''
        return s
