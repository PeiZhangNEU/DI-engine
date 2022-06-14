from ding.framework import task, EventEnum
import logging

from typing import TYPE_CHECKING, Dict, Callable

from ding.policy import Policy
from ding.framework.middleware import BattleEpisodeCollector, BattleStepCollector
from ding.framework.middleware.functional import ActorData
from ding.league.player import PlayerMeta
from threading import Lock
import queue
from easydict import EasyDict

if TYPE_CHECKING:
    from ding.league.v2.base_league import Job
    from ding.framework import BattleContext
    from ding.framework.middleware.league_learner import LearnerModel


class LeagueActor:

    def __init__(self, cfg: EasyDict, env_fn: Callable, policy_fn: Callable):
        self.cfg = cfg
        self.env_fn = env_fn
        self.env_num = env_fn().env_num
        self.policy_fn = policy_fn
        self.n_rollout_samples = self.cfg.policy.collect.get("n_rollout_samples") or 0
        self._collectors: Dict[str, BattleEpisodeCollector] = {}
        self.all_policies: Dict[str, "Policy.collect_function"] = {}
        task.on(EventEnum.COORDINATOR_DISPATCH_ACTOR_JOB.format(actor_id=task.router.node_id), self._on_league_job)
        task.on(EventEnum.LEARNER_SEND_MODEL, self._on_learner_model)
        self.job_queue = queue.Queue()
        self.model_dict = {}
        self.model_dict_lock = Lock()

    def _on_learner_model(self, learner_model: "LearnerModel"):
        """
        If get newest learner model, put it inside model_queue.
        """
        print("Actor receive model from learner \n")
        with self.model_dict_lock:
            self.model_dict[learner_model.player_id] = learner_model

    def _on_league_job(self, job: "Job"):
        """
        Deal with job distributed by coordinator, put it inside job_queue.
        """
        self.job_queue.put(job)

    def _get_collector(self, player_id: str, agent_num: int):
        if self._collectors.get(player_id):
            return self._collectors.get(player_id)
        cfg = self.cfg
        env = self.env_fn()
        collector = task.wrap(
            BattleEpisodeCollector(
                cfg.policy.collect.collector, env, self.n_rollout_samples, self.model_dict, self.all_policies, agent_num
            )
        )
        self._collectors[player_id] = collector
        return collector

    def _get_policy(self, player: "PlayerMeta") -> "Policy.collect_function":
        player_id = player.player_id
        if self.all_policies.get(player_id):
            return self.all_policies.get(player_id)
        policy: "Policy.collect_function" = self.policy_fn().collect_mode
        self.all_policies[player_id] = policy
        if "historical" in player.player_id:
            policy.load_state_dict(player.checkpoint.load())

        return policy

    def _get_job(self):
        if self.job_queue.empty():
            task.emit(EventEnum.ACTOR_GREETING, task.router.node_id)
        job = None

        try:
            job = self.job_queue.get(timeout=10)
        except queue.Empty:
            logging.warning("For actor_{}, no Job get from coordinator".format(task.router.node_id))

        return job

    def _get_current_policies(self, job):
        current_policies = []
        main_player: "PlayerMeta" = None
        for player in job.players:
            current_policies.append(self._get_policy(player))
            if player.player_id == job.launch_player:
                main_player = player
        assert main_player, "can not find active player, on actor: {}".format(task.router.node_id)

        if current_policies is not None:
            assert len(current_policies) > 1, "battle collector needs more than 1 policies"
            for p in current_policies:
                p.reset()
        else:
            raise RuntimeError('current_policies should not be None')

        return main_player, current_policies

    def __call__(self, ctx: "BattleContext"):

        ctx.job = self._get_job()
        if ctx.job is None:
            return

        collector = self._get_collector(ctx.job.launch_player, len(ctx.job.players))

        main_player, ctx.current_policies = self._get_current_policies(ctx.job)

        _default_n_episode = ctx.current_policies[0].get_attribute('cfg').collect.get('n_episode', None)
        if ctx.n_episode is None:
            if _default_n_episode is None:
                raise RuntimeError("Please specify collect n_episode")
            else:
                ctx.n_episode = _default_n_episode
        assert ctx.n_episode >= self.env_num, "Please make sure n_episode >= env_num"

        ctx.agent_num = len(ctx.current_policies)
        ctx.train_iter = main_player.total_agent_step
        ctx.episode_info = [[] for _ in range(ctx.agent_num)]
        ctx.remain_episode = ctx.n_episode
        while True:
            collector(ctx)

            if not ctx.job.is_eval and len(ctx.episodes[0]) > 0:
                actor_data = ActorData(env_step=ctx.total_envstep_count, train_data=ctx.episodes[0])
                task.emit(EventEnum.ACTOR_SEND_DATA.format(player=ctx.job.launch_player), actor_data)
                ctx.episodes = []
            if ctx.job_finish is True:
                ctx.job.result = [e['result'] for e in ctx.episode_info[0]]
                task.emit(EventEnum.ACTOR_FINISH_JOB, ctx.job)
                ctx.episode_info = [[] for _ in range(ctx.agent_num)]
                break


class StepLeagueActor:

    def __init__(self, cfg: EasyDict, env_fn: Callable, policy_fn: Callable):
        self.cfg = cfg
        self.env_fn = env_fn
        self.env_num = env_fn().env_num
        self.policy_fn = policy_fn
        self.n_rollout_samples = self.cfg.policy.collect.get("n_rollout_samples") or 0
        self.n_sample = self.cfg.policy.collect.get("n_sample") or 1
        self.unroll_len = self.cfg.policy.collect.get("unroll_len") or 1
        self._collectors: Dict[str, BattleEpisodeCollector] = {}
        self.all_policies: Dict[str, "Policy.collect_function"] = {}
        task.on(EventEnum.COORDINATOR_DISPATCH_ACTOR_JOB.format(actor_id=task.router.node_id), self._on_league_job)
        task.on(EventEnum.LEARNER_SEND_MODEL, self._on_learner_model)
        self.job_queue = queue.Queue()
        self.model_dict = {}
        self.model_dict_lock = Lock()

        # self._gae_estimator = gae_estimator(cfg, policy_fn().collect_mode)

    def _on_learner_model(self, learner_model: "LearnerModel"):
        """
        If get newest learner model, put it inside model_queue.
        """
        print('Actor got model \n')
        with self.model_dict_lock:
            self.model_dict[learner_model.player_id] = learner_model

    def _on_league_job(self, job: "Job"):
        """
        Deal with job distributed by coordinator, put it inside job_queue.
        """
        self.job_queue.put(job)

    def _get_collector(self, player_id: str, agent_num: int):
        if self._collectors.get(player_id):
            return self._collectors.get(player_id)
        cfg = self.cfg
        env = self.env_fn()
        collector = task.wrap(
            BattleStepCollector(
                cfg.policy.collect.collector, env, self.n_rollout_samples, self.model_dict, self.all_policies, agent_num
            )
        )
        self._collectors[player_id] = collector
        return collector

    def _get_policy(self, player: "PlayerMeta") -> "Policy.collect_function":
        player_id = player.player_id
        if self.all_policies.get(player_id):
            return self.all_policies.get(player_id)
        policy: "Policy.collect_function" = self.policy_fn().collect_mode
        self.all_policies[player_id] = policy
        if "historical" in player.player_id:
            policy.load_state_dict(player.checkpoint.load())

        return policy

    def _get_job(self):
        if self.job_queue.empty():
            task.emit(EventEnum.ACTOR_GREETING, task.router.node_id)
        job = None

        try:
            job = self.job_queue.get(timeout=10)
        except queue.Empty:
            logging.warning("For actor_{}, no Job get from coordinator".format(task.router.node_id))

        return job

    def _get_current_policies(self, job):
        current_policies = []
        main_player: "PlayerMeta" = None
        for player in job.players:
            current_policies.append(self._get_policy(player))
            if player.player_id == job.launch_player:
                main_player = player
        assert main_player, "can not find active player, on actor: {}".format(task.router.node_id)

        if current_policies is not None:
            assert len(current_policies) > 1, "battle collector needs more than 1 policies"
            for p in current_policies:
                p.reset()
        else:
            raise RuntimeError('current_policies should not be None')

        return main_player, current_policies

    def __call__(self, ctx: "BattleContext"):

        ctx.job = self._get_job()
        if ctx.job is None:
            return
        print('For actor, a job begin \n')

        collector = self._get_collector(ctx.job.launch_player, len(ctx.job.players))

        main_player, ctx.current_policies = self._get_current_policies(ctx.job)
        ctx.agent_num = len(ctx.current_policies)

        _default_n_episode = ctx.current_policies[0].get_attribute('cfg').collect.get('n_episode', None)
        if ctx.n_episode is None:
            if _default_n_episode is None:
                raise RuntimeError("Please specify collect n_episode")
            else:
                ctx.n_episode = _default_n_episode
        assert ctx.n_episode >= self.env_num, "Please make sure n_episode >= env_num"

        ctx.train_iter = main_player.total_agent_step
        ctx.episode_info = [[] for _ in range(ctx.agent_num)]
        ctx.remain_episode = ctx.n_episode
        ctx.n_sample = self.n_sample
        ctx.unroll_len = self.unroll_len
        while True:
            collector(ctx)

            if not ctx.job.is_eval and len(ctx.trajectories_list[0]) > 0:
                ctx.trajectories = ctx.trajectories_list[0]
                ctx.trajectory_end_idx = ctx.trajectory_end_idx_list[0]
                # self._gae_estimator(ctx)
                # actor_data = ActorData(env_step=ctx.total_envstep_count, train_data=ctx.train_data)
                actor_data = ActorData(env_step=ctx.total_envstep_count, train_data=ctx.trajectories)
                task.emit(EventEnum.ACTOR_SEND_DATA.format(player=ctx.job.launch_player), actor_data)
                print('Actor send data\n')

                ctx.trajectories_list = []
                ctx.trajectory_end_idx_list = []
                ctx.trajectories = []
                ctx.trajectory_end_idx = None

            if ctx.job_finish is True:
                ctx.job.result = [e['result'] for e in ctx.episode_info[0]]
                task.emit(EventEnum.ACTOR_FINISH_JOB, ctx.job)
                ctx.episode_info = [[] for _ in range(ctx.agent_num)]
                print('Actor job finish, send job\n')
                break