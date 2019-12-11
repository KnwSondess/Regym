import gym

from .gym_parser import parse_gym_environment
from .unity_parser import parse_unity_environment
from .parallel_env import ParallelEnv
from .vec_env import VecEnv
from .parallel_vec_env import ParallelVecEnv
from .utils import EnvironmentCreator
from .task import Task


def parse_environment(env_name, nbr_parallel_env=1, nbr_frame_stacking=1, wrapping_fn=None):
    '''
    Returns a regym.environments.Task by creating an environment derived from :param: env_name
    and extracting relevant information used to build regym.rl_algorithms.agents from the Task.
    If :param: env_name matches a registered OpenAI Gym environment it will create it from there
    If :param: env_name points to a (platform specific) UnityEnvironment executable, it will generate a Unity environment
    In the case of :param: env_name being detected as both an OpenAI Gym and Unity environmet, an error will be raised
    
    Note: :param wrapping_fn: is only usable with gym environment, not with Unity environments.

    :param env_name: String identifier for the environment to be created.
    :param nbr_parallel_env: number of environment to create and experience in parallel.
    :param nbr_frame_stacking: number of frame to stack as observations, on the depth channel.
    :param wrapping_fn: Function used to wrap the environment.
    :returns: Task created from :param: env_name
    '''
    is_gym_environment = any([env_name == spec.id for spec in gym.envs.registry.all()]) # Checks if :param: env_name was registered
    is_unity_environment = check_for_unity_executable(env_name)

    task = None
    if is_gym_environment and is_unity_environment: raise ValueError(f'{env_name} exists as both a Gym and an Unity environment. Rename Unity environment to remove duplicate problem.')
    elif is_gym_environment: 
        env = gym.make(env_name)
        if wrapping_fn is not None: env = wrapping_fn(env=env)
        task=parse_gym_environment(env)
    elif is_unity_environment: task=parse_unity_environment(env_name)
    else: raise ValueError('Environment \'{env_name}\' was not recognized as either a Gym nor a Unity environment')

    task.env.close()
    
    env_creator = EnvironmentCreator(env_name, is_unity_environment, is_gym_environment, wrapping_fn=wrapping_fn)
    
    task = Task(task.name, ParallelEnv(env_creator, nbr_parallel_env, nbr_frame_stacking), task.state_space_size, task.action_space_size, task.observation_shape, task.observation_type, task.action_dim, task.action_type, task.hash_function)
    #task = Task(task.name, VecEnv(env_creator, nbr_parallel_env, nbr_frame_stacking), task.state_space_size, task.action_space_size, task.observation_shape, task.observation_type, task.action_dim, task.action_type, task.hash_function)
    #task = Task(task.name, ParallelVecEnv(env_creator, nbr_parallel_env, nbr_frame_stacking), task.state_space_size, task.action_space_size, task.observation_shape, task.observation_type, task.action_dim, task.action_type, task.hash_function)
    
    return task

def check_for_unity_executable(env_name):
    '''
    Checks if :param: env_name points to a Unity Executable
    :param env_name: String identifier for the environment to be created
    :returns: Boolean whether :param: env_name is a Unity executable
    '''
    import os, platform
    valid_extensions = {'Linux': '.x86_64', 'Darwin': '.app', 'Windows': '.exe'}
    return os.path.isfile(env_name + valid_extensions[platform.system()])
