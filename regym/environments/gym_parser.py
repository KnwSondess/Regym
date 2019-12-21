import itertools
import numpy as np
from gym.spaces import Box, Discrete, MultiDiscrete, Tuple

from gym_rock_paper_scissors.envs.one_hot_space import OneHotEncoding

from .task import Task


def parse_gym_environment(env, name=None):
    '''
    Generates a regym.environments.Task by extracting information from the
    already built :param: env.

    This function makes the following Assumptions from :param: env:
        - Observation / Action space (it's geometry, dimensionality) are identical for all agents

    :param env: Environment following OpenAI Gym interface
    :param name: String identifier for the name
    :returns: Task created from :param: env named :param: name
    '''
    name = env.spec.id if name is None else name
    action_dims, action_type = get_action_dimensions_and_type(env)
    observation_shape, observation_type = get_observation_dimensions_and_type(env)
    state_space_size = env.state_space_size if hasattr(env, 'state_space_size') else None
    action_space_size = env.action_space_size if hasattr(env, 'action_space_size') else None
    hash_function = env.hash_state if hasattr(env, 'hash_state') else None
    return Task(name, env, None, state_space_size, action_space_size, observation_shape, observation_type, action_dims, action_type, hash_function)


def get_observation_dimensions_and_type(env):
    def parse_dimension_space(space):
        if isinstance(space, OneHotEncoding): return space.size, 'Discrete'
        elif isinstance(space, Discrete): return space.n, 'Discrete'
        elif isinstance(space, Box): return space.shape, 'Continuous'
        elif isinstance(space, Tuple): return sum([parse_dimension_space(s)[0] for s in space.spaces]), parse_dimension_space(space.spaces[0])[1]
        raise ValueError('Unknown observation space: {}'.format(space))

    if hasattr(env.observation_space, 'spaces'): return parse_dimension_space(env.observation_space.spaces[0]) # Multi agent environment
    else: return parse_dimension_space(env.observation_space) # Single agent environment


def get_action_dimensions_and_type(env):
    def parse_dimension_space(space):
        if isinstance(space, Discrete): return space.n, 'Discrete'
        elif isinstance(space, MultiDiscrete): return compute_multidiscrete_space_size(space.nvec), 'Discrete'
        elif isinstance(space, Box): return space.shape[0], 'Continuous'
        else: raise ValueError('Unknown action space: {}'.format(space))

    if hasattr(env.action_space, 'spaces'): return parse_dimension_space(env.action_space.spaces[0]) # Multi agent environment
    else: return parse_dimension_space(env.action_space) # Single agent environment


def compute_multidiscrete_space_size(flattened_multidiscrete_space):
    """
    Computes size of the combinatorial space generated by :param: flattened_multidiscrete_space

    :param multidiscrete_action_space: gym.spaces.MultiDiscrete space
    :returns: Size of 'flattened' :param: flattened_multidiscrete_space
    """
    possible_vals = [range(_num) for _num in flattened_multidiscrete_space]
    return len([list(_action) for _action in itertools.product(*possible_vals)])
