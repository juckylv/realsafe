import tensorflow as tf
import numpy as np
import multiprocessing as mp
import ctypes
import collections
import cv2

from realsafe.attack.base import BatchAttack
from realsafe.attack.utils import mean_square_distance


class AttackCtx(object):
    def __init__(self, attacker, index, xs_batch_array, ys_batch_array, dist_array,
                 starting_points, ys, ys_target, pipe):
        self.x_dtype, self.y_dtype = attacker.model.x_dtype.as_numpy_dtype, attacker.model.y_dtype.as_numpy_dtype
        self.batch_size, self.x_shape = attacker.batch_size, attacker.model.x_shape
        self.goal = attacker.goal
        self.index = index
        self.x_min, self.x_max = attacker.model.x_min, attacker.model.x_max
        self.y = None if ys is None else ys[index]
        self.y_target = None if ys_target is None else ys_target[index]
        self.spherical_step = attacker.spherical_step
        self.source_step = attacker.source_step
        self.step_adaptation = attacker.step_adaptation
        self.max_queries, self.max_directions = attacker.max_queries, attacker.max_directions
        self.pipe = pipe
        self.starting_point = starting_points[index]
        self.dimension_reduction = attacker.dimension_reduction

        self._xs_batch_array, self._ys_batch_array = xs_batch_array, ys_batch_array
        self._dist_array = dist_array

        self.logs = [] if attacker.logger else None

    def _is_adversarial(self, label):
        if self.goal == 'ut' or self.goal == 'tm':
            return label != self.y
        else:
            return label == self.y_target

    def worker(self):
        task = self.run()
        next(task)

        while True:
            self.pipe.recv()
            try:
                next(task)
                self.pipe.send((False, self.logs.copy() if self.logs else []))
                self.logs = [] if self.logs is not None else None
            except StopIteration:
                self.pipe.send((True, self.logs.copy() if self.logs else []))
                return

    def run(self):
        index = self.index
        xs_batch = np.frombuffer(self._xs_batch_array, dtype=self.x_dtype).reshape((self.batch_size, *self.x_shape))
        ys_batch = np.frombuffer(self._ys_batch_array, dtype=self.y_dtype).reshape((self.batch_size,))

        x = xs_batch[index].copy()

        yield
        if self._is_adversarial(ys_batch[index]):
            if self.logs is not None:
                self.logs.append('{}: The original image is already adversarial'.format(index))
            return

        x_adv = self.starting_point
        dist = mean_square_distance(x_adv, x, self.x_min, self.x_max)
        dist_per_query = np.frombuffer(self._dist_array, dtype=np.float).reshape((self.batch_size, -1))
        stats_spherical_adversarial = collections.deque(maxlen=100)
        stats_step_adversarial = collections.deque(maxlen=30)

        xs_batch[index] = x_adv
        yield
        x_adv_label = ys_batch[index]

        if self.logs is not None:
            self.logs.append('{}: step {}, {:.5e}, prediction={}, stepsizes={:.1e}/{:.1e}: {}'.format(
                index, 0, x_adv_label, dist, self.spherical_step, self.source_step, ''
            ))
        dist_per_query[self.index][0] = dist

        step, queries, last_queries = 0, 0, 0
        while True:
            step += 1

            unnormalized_source_direction = x - x_adv
            source_norm = np.linalg.norm(unnormalized_source_direction)
            source_direction = unnormalized_source_direction / source_norm

            do_spherical = (step % 10 == 0)

            for _ in range(self.max_directions):
                if self.dimension_reduction:
                    assert len(self.x_shape) == 3
                    perturbation_shape = (*self.dimension_reduction, self.x_shape[2])
                    perturbation = np.random.normal(0.0, 1.0, perturbation_shape).astype(self.x_dtype)
                    perturbation = cv2.resize(perturbation, self.x_shape[:2])
                else:
                    perturbation = np.random.normal(0.0, 1.0, self.x_shape).astype(self.x_dtype)
                dot = np.vdot(perturbation, source_direction)
                perturbation -= dot * source_direction
                perturbation *= self.spherical_step * source_norm / np.linalg.norm(perturbation)

                D = 1 / np.sqrt(self.spherical_step ** 2.0 + 1)
                direction = perturbation - unnormalized_source_direction
                spherical_candidate = np.clip(x + D * direction, self.x_min, self.x_max)

                new_source_direction = x - spherical_candidate
                new_source_direction_norm = np.linalg.norm(new_source_direction)
                length = self.source_step * source_norm

                deviation = new_source_direction_norm - source_norm
                length = max(0, length + deviation) / new_source_direction_norm
                candidate = np.clip(spherical_candidate + length * new_source_direction, self.x_min, self.x_max)

                if do_spherical:
                    xs_batch[index] = spherical_candidate
                    yield
                    spherical_is_adversarial = self._is_adversarial(ys_batch[index])

                    queries += 1
                    if queries == self.max_queries:
                        xs_batch[index] = x_adv
                        dist_per_query[self.index][last_queries:min(queries + 1, self.max_queries + 1)] = dist
                        return
                    stats_spherical_adversarial.appendleft(spherical_is_adversarial)

                    if not spherical_is_adversarial:
                        continue

                xs_batch[index] = candidate
                yield
                is_adversarial = self._is_adversarial(ys_batch[index])

                queries += 1
                if queries == self.max_queries:
                    xs_batch[index] = x_adv
                    dist_per_query[self.index][last_queries:min(queries + 1, self.max_queries + 1)] = dist
                    return

                if do_spherical:
                    stats_step_adversarial.appendleft(is_adversarial)

                if not is_adversarial:
                    continue

                new_x_adv = candidate
                new_dist = mean_square_distance(new_x_adv, x, self.x_min, self.x_max)
                break
            else:
                new_x_adv = None

            dist_per_query[self.index][last_queries:min(queries + 1, self.max_queries + 1)] = dist

            message = ''
            if new_x_adv is not None:
                abs_improvement = dist - new_dist
                rel_improvement = abs_improvement / dist
                message = 'd. reduced by {:.2f}% ({:.4e})'.format(rel_improvement * 100, abs_improvement)
                x_adv_label, x_adv, dist = ys_batch[index], new_x_adv, new_dist

            if self.logs is not None:
                self.logs.append('{}: step {}, {:.5e}, prediction={}, stepsizes={:.1e}/{:.1e}: {}'.format(
                    index, step, x_adv_label, dist, self.spherical_step, self.source_step, message
                ))

            if len(stats_step_adversarial) == stats_step_adversarial.maxlen and \
                    len(stats_spherical_adversarial) == stats_spherical_adversarial.maxlen:
                p_spherical = np.mean(stats_spherical_adversarial)
                p_step = np.mean(stats_step_adversarial)
                n_spherical = len(stats_spherical_adversarial)
                n_step = len(stats_step_adversarial)

                if p_spherical > 0.5:
                    message = 'Boundary too linear, increasing steps:'
                    self.spherical_step *= self.step_adaptation
                    self.source_step *= self.step_adaptation
                elif p_spherical < 0.2:
                    message = 'Boundary too non-linear, decreasing steps:'
                    self.spherical_step /= self.step_adaptation
                    self.source_step /= self.step_adaptation
                else:
                    message = None

                if message is not None:
                    stats_spherical_adversarial.clear()
                    if self.logs is not None:
                        self.logs.append('{}: {} {:.2f} ({:3d}), {:.2f} ({:3d})'.format(
                            index, message, p_spherical, n_spherical, p_step, n_step
                        ))

                if p_step > 0.5:
                    message = 'Success rate too high, increasing source step:'
                    self.source_step *= self.step_adaptation
                elif p_step < 0.2:
                    message = 'Success rate too low, decreasing source step:'
                    self.source_step /= self.step_adaptation
                else:
                    message = None

                if message is not None:
                    stats_step_adversarial.clear()
                    if self.logs is not None:
                        self.logs.append('{}: {} {:.2f} ({:3d}), {:.2f} ({:3d})'.format(
                            index, message, p_spherical, n_spherical, p_step, n_step
                        ))

            last_queries = queries
            if queries == self.max_queries:
                xs_batch[index] = x_adv
                dist_per_query[self.index][last_queries:min(queries + 1, self.max_queries + 1)] = dist
                return


class Boundary(BatchAttack):
    '''
    Boundary
    A black-box decision-based method.

    Supported distance metric: `l_2`
    Supported goal: `t`, `tm`, `ut`
    Supported config parameters:
    - `magnitude`: max distortion, could be either a float number or a numpy float number array with shape of

    References:
    [1]
    '''

    def __init__(self, model, batch_size, goal, session, dimension_reduction=None):
        self.model, self.batch_size, self.goal, self._session = model, batch_size, goal, session
        self.dimension_reduction = dimension_reduction

        self.xs_ph = tf.placeholder(model.x_dtype, (self.batch_size, *self.model.x_shape))
        self.labels = self.model.labels(self.xs_ph)

        self.logger = None
        self.details = {}

    def config(self, **kwargs):
        if 'starting_points' in kwargs:
            self.starting_points = kwargs['starting_points']

        if 'max_queries' in kwargs:
            self.max_queries = kwargs['max_queries']
        if 'max_directions' in kwargs:
            self.max_directions = kwargs['max_directions']

        if 'spherical_step' in kwargs:
            self.spherical_step = kwargs['spherical_step']
        if 'source_step' in kwargs:
            self.source_step = kwargs['source_step']
        if 'step_adaptation' in kwargs:
            self.step_adaptation = kwargs['step_adaptation']

        if 'logger' in kwargs:
            self.logger = kwargs['logger']

    def batch_attack(self, xs, ys=None, ys_target=None):
        xs_batch_array = mp.RawArray(ctypes.c_byte, np.array(xs).astype(self.model.x_dtype.as_numpy_dtype).nbytes)
        xs_shape = (self.batch_size, *self.model.x_shape)
        xs_batch = np.frombuffer(xs_batch_array, dtype=self.model.x_dtype.as_numpy_dtype).reshape(xs_shape)
        xs_batch[:] = xs
        self.details['dist_per_query'] = np.zeros((self.batch_size, self.max_queries + 1), dtype=np.float)
        dist_array = mp.RawArray(ctypes.c_byte, self.details['dist_per_query'].nbytes)

        lbs = ys_target if ys is None else ys
        ys_batch_array = mp.RawArray(ctypes.c_byte, np.array(lbs).astype(self.model.y_dtype.as_numpy_dtype).nbytes)
        ys_batch_shape = (self.batch_size,)
        ys_batch = np.frombuffer(ys_batch_array, dtype=self.model.y_dtype.as_numpy_dtype).reshape(ys_batch_shape)

        workers = []
        for index in range(self.batch_size):
            local, remote = mp.Pipe()
            ctx = AttackCtx(self, index, xs_batch_array, ys_batch_array, dist_array,
                            self.starting_points, ys, ys_target, remote)
            worker = mp.Process(target=AttackCtx.worker, args=(ctx,))
            worker.start()
            workers.append((worker, local))

        while True:
            ys_batch[:] = self._session.run(self.labels, feed_dict={self.xs_ph: xs_batch})
            for _, pipe in workers:
                if not pipe.closed:
                    pipe.send(())
            flag = False
            for _, pipe in workers:
                if not pipe.closed:
                    stop, logs = pipe.recv()
                    if self.logger:
                        for log in logs:
                            self.logger.info(log)
                    if stop:
                        pipe.close()
                    else:
                        flag = True
            if not flag:
                break

        dist_per_query = np.frombuffer(dist_array, dtype=np.float).reshape((self.batch_size, -1))
        np.copyto(self.details['dist_per_query'], dist_per_query)
        return xs_batch.copy()
