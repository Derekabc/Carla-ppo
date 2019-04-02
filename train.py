import os
import random
import re
import shutil
import time
from collections import deque

import cv2
import gym
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from skimage import transform

from ppo import PPO
from vae.models import ConvVAE, MlpVAE
from CarlaEnv.carla_env import CarlaEnv
from CarlaEnv.wrappers import angle_diff, carla_as_array
from utils import VideoRecorder, compute_gae, discount, RunningMeanVar

def preprocess_frame(frame):
    frame = frame.astype(np.float32) / 255.0
    return frame

def reward_fn_1(env):
    if env.terminal_state == True: return -10

    velocity = env.vehicle.get_velocity()
    speed = np.sqrt(velocity.x**2 + velocity.y**2)
    reward = speed #* 0.001
    return reward

def reward_fn(env):
    terminal_state = env.terminal_state

    # If speed is less than 1 after 5s, stop
    speed = env.vehicle.get_speed()
    if time.time() - env.start_t > 5.0 and speed < 1.0:
        terminal_state = True

    # If heading is oposite, stop
    transform = env.vehicle.get_transform()
    waypoint = env.world.map.get_waypoint(transform.location, project_to_road=True) # Get closest waypoint
    #world.debug.draw_point(wp_loc, life_time=1.0)
    loc, wp_loc = carla_as_array(waypoint.transform.location), carla_as_array(transform.location)
    distance_from_center = np.linalg.norm(loc[:2] - wp_loc[:2])

    fwd = transform.rotation.get_forward_vector()
    wp_fwd = waypoint.transform.rotation.get_forward_vector()
    angle = angle_diff(carla_as_array(fwd), carla_as_array(wp_fwd))

    if angle > np.pi/2 or angle < -np.pi/2 or distance_from_center > 3.0:
        terminal_state = True

    reward = 0
    if terminal_state == True:
        env.terminal_state = True
        reward -= 10
    else:
        #if 3.6 * speed < 20.0: # No reward over 20 kmh
        #    reward += env.vehicle.control.throttle
        norm_speed = 3.6 * speed / 20.0
        if norm_speed > 1.0:
            reward += (1.0 - norm_speed) * 3
        else:
            reward += norm_speed * 3
        reward -= distance_from_center

    env.extra_info.extend([
        "Distance from center: %.2f" % distance_from_center,
        "Angle difference: %.2f" % np.rad2deg(angle),
        "Wrong way" if (np.rad2deg(angle) > 90 or np.rad2deg(angle) < -90) else "Right way",
        "Reward: %.4f" % reward
    ])
    return reward

def create_encode_state_fn(vae):
    def encode_state(env):
        """
            Function that encodes the current state of
            the environment into some feature vector.
        """
        frame = preprocess_frame(env.observation)
        encoded_state = vae.encode([frame])[0]
        
        measurements = []
        measurements.append(env.vehicle.control.steer)
        measurements.append(env.vehicle.control.throttle)
        measurements.append(env.vehicle.get_speed())
        
        encoded_state = np.append(encoded_state, measurements)
        
        return encoded_state
    return encode_state

def make_env(title=None, frame_skip=0, encode_state_fn=None):
    env = CarlaEnv(obs_res=(160, 80), encode_state_fn=encode_state_fn, reward_fn=reward_fn)
    env.seed(0)
    return env

intrinsic_rmv = RunningMeanVar()

def test_agent(test_env, model, video_filename=None):
    # Init test env
    state, terminal, total_reward = test_env.reset(), False, 0
    rendered_frame = test_env.render(mode="rgb_array")

    # Init video recording
    if video_filename is not None:
        video_recorder = VideoRecorder(video_filename, frame_size=rendered_frame.shape)
        video_recorder.add_frame(rendered_frame)
    else:
        video_recorder = None

    episode_idx = model.get_episode_idx()

    # While non-terminal state
    while not terminal:
        test_env.extra_info.append("Episode {}".format(episode_idx))
        test_env.extra_info.append("Running eval...".format(episode_idx))
        test_env.extra_info.append("")

        # Take deterministic actions at test time (noise_scale=0)
        action, _, _ = model.predict([state], greedy=True)
        state, reward, terminal, info = test_env.step(action)

        if info["closed"] == True:
            break

        # Add frame
        rendered_frame = test_env.render(mode="rgb_array")
        if video_recorder: video_recorder.add_frame(rendered_frame)
        total_reward += reward

    # Release video
    if video_recorder:
        video_recorder.release()

    if info["closed"] == True:
        exit(0)
    
    return total_reward, 0#test_env.reward

def train(params, model_name, save_interval=10, eval_interval=10, record_eval=True, restart=False):
    # Traning parameters
    learning_rate    = params["learning_rate"]
    lr_decay         = params["lr_decay"]
    discount_factor  = params["discount_factor"]
    gae_lambda       = params["gae_lambda"]
    ppo_epsilon      = params["ppo_epsilon"]
    value_scale      = params["value_scale"]
    entropy_scale    = params["entropy_scale"]
    horizon          = params["horizon"]
    num_epochs       = params["num_epochs"]
    num_episodes     = params["num_episodes"]
    batch_size       = params["batch_size"]
    vae_model        = params["vae_model"]
    vae_model_type   = params["vae_model_type"]
    vae_z_dim        = params["vae_z_dim"]

    if vae_z_dim is None:      vae_z_dim = params["vae_z_dim"] = int(re.findall("zdim(\d+)", vae_model)[0])
    if vae_model_type is None: vae_model_type = params["vae_model_type"] = "mlp" if "mlp" in vae_model else "cnn"
    VAEClass = MlpVAE if vae_model_type == "mlp" else ConvVAE

    print("")
    print("Training parameters:")
    for k, v, in params.items(): print(f"  {k}: {v}")
    print("")

    vae_input_shape = np.array([80, 160, 3])

    # Load pre-trained variational autoencoder
    vae = VAEClass(input_shape=vae_input_shape,
                   z_dim=vae_z_dim, models_dir="vae",
                   model_name=vae_model,
                   training=False)
    vae.init_session(init_logging=False)
    if not vae.load_latest_checkpoint():
        raise Exception("Failed to load VAE")

    # State encoding fn
    with_measurements = True
    stack = None
    encode_state_fn = create_encode_state_fn(vae)

    # Create env
    print("Creating environment")
    env      = make_env(model_name, frame_skip=0, encode_state_fn=encode_state_fn)
    #test_env = make_env(model_name + " (Test)", encode_state_fn=encode_state_fn)

    # Environment constants
    input_shape  = np.array([vae_z_dim])
    if with_measurements:      input_shape[0] += 3
    if isinstance(stack, int): input_shape[0] *= stack
    num_actions      = env.action_space.shape[0]
    action_min       = env.action_space.low
    action_max       = env.action_space.high

    # Create model
    print("Creating model")
    model = PPO(input_shape, num_actions, action_min, action_max,
                learning_rate=learning_rate, lr_decay=lr_decay, epsilon=ppo_epsilon, initial_std=0.1,
                value_scale=value_scale, entropy_scale=entropy_scale,
                int_coeff=0.0, ext_coeff=1.0,
                output_dir=os.path.join("models", model_name))

    # Prompt to load existing model if any
    if not restart:
        if os.path.isdir(model.log_dir) and len(os.listdir(model.log_dir)) > 0:
            answer = input("Model \"{}\" already exists. Do you wish to continue (C) or restart training (R)? ".format(model_name))
            if answer.upper() == "C":
                model.load_latest_checkpoint()
            elif answer.upper() == "R":
                restart = True
            else:
                raise Exception("There are already log files for model \"{}\". Please delete it or change model_name and try again".format(model_name))
    if restart:
        shutil.rmtree(model.output_dir)
        for d in model.dirs:
            os.makedirs(d)
    model.init_logging()
    model.write_dict_to_summary("hyperparameters", params, 0)

    # For every episode
    while num_episodes <= 0 or model.get_episode_idx() < num_episodes:
        episode_idx = model.get_episode_idx()

        # Save model periodically
        if episode_idx % save_interval == 0:
            model.save()
        
        # Run evaluation periodically
        if episode_idx % eval_interval == 0:
            video_filename = os.path.join(model.video_dir, "episode{}.avi".format(episode_idx))
            eval_reward, eval_score = test_agent(env, model, video_filename=video_filename)
            model.write_value_to_summary("eval/score",  eval_score,  episode_idx)
            model.write_value_to_summary("eval/reward", eval_reward, episode_idx)

        # Reset environment
        state, terminal_state, total_reward, total_value = env.reset(), False, 0, 0
        
        # While episode not done
        print(f"Episode {episode_idx} (Step {model.get_train_step_idx()})")
        while not terminal_state:
            states, taken_actions, extrinsic_values, intrinsic_values, extrinsic_rewards, dones = [], [], [], [], [], []
            for _ in range(horizon):
                action, extrinsic_value, intrinsic_value = model.predict([state], write_to_summary=True)

                # Perform action
                env.extra_info.append("Episode {}".format(episode_idx))
                env.extra_info.append("Training...".format(episode_idx))
                env.extra_info.append("")
                new_state, reward, terminal_state, info = env.step(action)

                if info["closed"] == True:
                    exit(0)
                    
                env.render()
                total_reward += reward

                # Store state, action and reward
                states.append(state)         # [T, *input_shape]
                taken_actions.append(action) # [T,  num_actions]
                intrinsic_values.append(intrinsic_value)         # [T]
                extrinsic_values.append(extrinsic_value)         # [T]
                extrinsic_rewards.append(reward)       # [T]
                dones.append(terminal_state) # [T]
                state = new_state

                if terminal_state:
                    break

            states        = np.array(states)

            # Calculate last value (bootstrap value)
            _, last_extrinsic_value, last_intrinsic_value = model.predict([state]) # []

            # Compute GAE
            extrinsic_advantages = compute_gae(extrinsic_rewards, extrinsic_values, last_extrinsic_value, dones, discount_factor, gae_lambda)
            extrinsic_returns = extrinsic_advantages + extrinsic_values
            extrinsic_advantages = (extrinsic_advantages - extrinsic_advantages.mean()) / (extrinsic_advantages.std() + 1e-8)

            # Get intrinsic rewards for input states
            intrinsic_rewards = model.sess.run(model.intrinsic_reward, feed_dict={model.input_states: states})

            # Normalize intrinsic rewards with running variance
            for v in discount(intrinsic_rewards, discount_factor).flatten():
                intrinsic_rmv.update(v)
            intrinsic_rewards = intrinsic_rewards / np.sqrt(intrinsic_rmv.var)
            intrinsic_advantages = compute_gae(intrinsic_rewards, intrinsic_values, last_intrinsic_value, False, discount_factor, gae_lambda)
            intrinsic_returns = intrinsic_advantages + intrinsic_values
            intrinsic_advantages = (intrinsic_advantages - intrinsic_advantages.mean()) / (intrinsic_advantages.std() + 1e-8)

            # Flatten arrays
            taken_actions = np.array(taken_actions)
            extrinsic_returns       = np.array(extrinsic_returns)
            intrinsic_returns       = np.array(intrinsic_returns)
            extrinsic_advantages    = np.array(extrinsic_advantages)
            intrinsic_advantages    = np.array(intrinsic_advantages)

            T = len(extrinsic_rewards)
            assert states.shape == (T, *input_shape)
            assert taken_actions.shape == (T, num_actions)
            assert extrinsic_returns.shape == (T,)
            assert intrinsic_returns.shape == (T,)
            assert extrinsic_advantages.shape == (T,)
            assert intrinsic_advantages.shape == (T,)

            # Train for some number of epochs
            model.update_old_policy() # θ_old <- θ
            for _ in range(num_epochs):
                num_samples = len(states)
                indices = np.arange(num_samples)
                np.random.shuffle(indices)
                for i in range(int(np.ceil(num_samples / batch_size))):
                    # Sample mini-batch randomly
                    begin = i * batch_size
                    end   = begin + batch_size
                    if end > num_samples:
                        end = None
                    mb_idx = indices[begin:end]

                    # Optimize network
                    model.train(states[mb_idx], taken_actions[mb_idx],
                                extrinsic_returns[mb_idx], intrinsic_returns[mb_idx],
                                extrinsic_advantages[mb_idx], intrinsic_advantages[mb_idx])

        # Write episodic values
        model.write_value_to_summary("train/score", 0, episode_idx)
        model.write_value_to_summary("train/reward", total_reward, episode_idx)
        model.write_value_to_summary("train/value", total_value, episode_idx)
        model.write_episodic_summaries()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trains an agent in a the RoadFollowing environment")

    # Hyper parameters
    parser.add_argument("--learning_rate", type=float, default=1e-4)
    parser.add_argument("--lr_decay", type=float, default=1.0)#0.98)
    parser.add_argument("--discount_factor", type=float, default=0.99)
    parser.add_argument("--gae_lambda", type=float, default=0.95)
    parser.add_argument("--ppo_epsilon", type=float, default=0.2)
    parser.add_argument("--value_scale", type=float, default=1.0)
    parser.add_argument("--entropy_scale", type=float, default=0.01)
    parser.add_argument("--horizon", type=int, default=128)
    parser.add_argument("--num_epochs", type=int, default=3)
    parser.add_argument("--num_episodes", type=int, default=0)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--vae_model", type=str, default="bce_cnn_zdim64_beta1_kl_tolerance0.0_data")
    parser.add_argument("--vae_model_type", type=str, default=None)
    parser.add_argument("--vae_z_dim", type=int, default=None)

    # Training vars
    parser.add_argument("--model_name", type=str, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--save_interval", type=int, default=5)
    parser.add_argument("--eval_interval", type=int, default=5)
    parser.add_argument("--record_eval", type=bool, default=True)
    parser.add_argument("-restart", action="store_true")

    params = vars(parser.parse_args())

    # Remove non-hyperparameters
    model_name = params["model_name"]; del params["model_name"]
    seed = params["seed"]; del params["seed"]
    save_interval = params["save_interval"]; del params["save_interval"]
    eval_interval = params["eval_interval"]; del params["eval_interval"]
    record_eval = params["record_eval"]; del params["record_eval"]
    restart = params["restart"]; del params["restart"]

    # Reset tf and set seed
    tf.reset_default_graph()
    if isinstance(seed, int):
        tf.random.set_random_seed(seed)
        np.random.seed(seed)
        random.seed(0)

    # Call main func
    train(params, model_name,
          save_interval=save_interval,
          eval_interval=eval_interval,
          record_eval=record_eval,
          restart=restart)
