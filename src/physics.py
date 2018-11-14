import collections
import itertools
import logging
import time
import warnings

import google.protobuf.json_format
import numpy as np
import numpy.linalg as linalg
import scipy.spatial
import scipy.special
from scipy.integrate import *

import orbitx_pb2 as protos
import common

# Higher values of this result in faster simulation but more chance of missing
# a collision. Units of this are in seconds.
MAX_STEP_SIZE = 1.0
FUTURE_WORK_SIZE = 10.0
warnings.simplefilter('error')  # Raise exception on numpy RuntimeWarning
log = logging.getLogger()

# Note on variable naming:
# a lowercase single letter, like `x`, is likely a scalar
# an uppercase single letter, like `X`, is likely a 1D row vector of scalars
# `y` is either the y position, or a vector of the form [X, Y, DX, DY]. i.e. 2D
# `y_1d` is the 1D row vector flattened version of the above `y` 2D vector,
#     which is required by `solve_ivp` inputs and outputs

# Note on the np.array family of functions
# https://stackoverflow.com/a/52103839/1333978
# Basically, it can sometimes be important in this module whether a call to
# np.array() is copying something, or changing the dtype, etc.

class PhysicsEntity(object):
    def __init__(self, entity):
        assert isinstance(entity, protos.Entity)
        self.name = entity.name
        self.pos = np.asarray([entity.x, entity.y])
        self.R = entity.r
        self.v = np.asarray([entity.vx, entity.vy])
        self.m = entity.mass
        self.spin = entity.spin
        self.heading = entity.heading

    def as_proto(self):
        return protos.Entity(
            name=self.name,
            x=self.pos[0],
            y=self.pos[1],
            vx=self.v[0],
            vy=self.v[1],
            r=self.R,
            mass=self.m,
            spin=self.spin,
            heading=self.heading
        )


class PEngine(object):
    """Physics Engine class. Encapsulates simulating physical state.

    Methods beginning with an underscore are not part of the API and change!

    Example usage:
    pe = PEngine(flight_savefile)
    state = pe.get_state()
    pe.set_time_acceleration(20)
    # Simulates 20 * [amount of time elapsed since last get_state() call]:
    state = pe.get_state()
    """

    def __init__(self, flight_savefile=None, mirror_state=None):
        # A PhysicalState, but no x, y, vx, or vy. That's handled by get_state.
        self._template_physical_state = protos.PhysicalState()
        # TODO: get rid of this check by only taking in a PhysicalState as arg.
        if flight_savefile:
            self.Load_json(flight_savefile)
        elif mirror_state:
            self.set_state(mirror_state())
        else:
            raise ValueError('need either a file or a lead server')
        self._reset_solutions()
        self._time_acceleration = 1.0
        self._last_state_request_time = time.monotonic()
        if len(self._template_physical_state.entities) > 1:
            self._events_function = _smallest_altitude_event
        else:
            # If there's only one entity, make a no-op events function
            self._events_function = lambda t, y: 1

    def set_time_acceleration(self, time_acceleration):
        """Change the speed at which this PEngine simulates at."""
        if time_acceleration <= 0:
            log.error(f'Time acceleration {time_acceleration} must be > 0')
            return
        self._time_acceleration = time_acceleration
        self._reset_solutions()

    def _time_elapsed(self):
        time_elapsed = time.monotonic() - self._last_state_request_time
        self._last_state_request_time = time.monotonic()
        return max(time_elapsed, 0.001)

    def _reset_solutions(self):
        # self._solutions is effectively a cache of ODE solutions, which
        # can be evaluated continuously for any value of t.
        # If something happens to invalidate this cache, clear the cache.
        self._solutions = collections.deque(maxlen=100)

    def _physics_entity_at(self, y, i):
        """Returns a PhysicsEntity constructed from the i'th entity."""
        physics_entity = PhysicsEntity(
            self._template_physical_state.entities[i])
        physics_entity.pos = np.array([y[0][i], y[1][i]])
        physics_entity.v = np.array([y[2][i], y[3][i]])
        return physics_entity

    def _merge_physics_entity_into(self, physics_entity, y, i):
        """Inverse of _physics_entity_at, merges a physics_entity into y."""
        y[0][i], y[1][i] = physics_entity.pos
        y[2][i], y[3][i] = physics_entity.v
        return y

    def Load_json(self, file):
        with open(file) as f:
            data = f.read()
        read_state = protos.PhysicalState()
        google.protobuf.json_format.Parse(data, read_state)
        self.set_state(read_state)

    def Save_json(self, file=common.AUTOSAVE_SAVEFILE):
        with open(file, 'w') as outfile:
            outfile.write(google.protobuf.json_format.MessageToJson(
                self._state_from_y(
                    self._template_physical_state.timestamp,
                    [self.X, self.Y, self.DX, self.DY]),
                including_default_value_fields=False))

    def set_state(self, physical_state):
        self.X = np.array([entity.x for entity in physical_state.entities]
                          ).astype(np.float64)
        self.Y = np.array([entity.y for entity in physical_state.entities]
                          ).astype(np.float64)
        self.DX = np.array([entity.vx for entity in physical_state.entities]
                           ).astype(np.float64)
        self.DY = np.array([entity.vy for entity in physical_state.entities]
                           ).astype(np.float64)
        self.R = np.array([entity.r for entity in physical_state.entities]
                          ).astype(np.float64)
        self.M = np.array([entity.mass for entity in physical_state.entities]
                          ).astype(np.float64)
        self.Fuel = \
            np.array([entity.fuel for entity in physical_state.entities]
                     ).astype(np.float64)
        _smallest_altitude_event.radii = self.R.reshape(1, -1)  # Column vector

        # Don't store positions and velocities in the physical_state,
        # it should only be returned from get_state()
        self._template_physical_state.CopyFrom(physical_state)
        for entity in self._template_physical_state.entities:
            entity.ClearField('x')
            entity.ClearField('y')
            entity.ClearField('vx')
            entity.ClearField('vy')

        self._reset_solutions()

    def _resolve_collision(self, e1, e2):
        # Resolve a collision by:
        # 1. calculating positions and velocities of the two entities
        # 2. do a 1D collision calculation along the normal between the two
        # 3. recombine the velocity vectors

        norm = e1.pos - e2.pos
        unit_norm = norm / np.linalg.norm(norm)
        # The unit tangent is perpendicular to the unit normal vector
        unit_tang = np.asarray([-unit_norm[1], unit_norm[0]])

        # Calculate both normal and tangent velocities for both entities
        v1n = scipy.dot(unit_norm, e1.v)
        v1t = scipy.dot(unit_tang, e1.v)
        v2n = scipy.dot(unit_norm, e2.v)
        v2t = scipy.dot(unit_tang, e2.v)

        # Use https://en.wikipedia.org/wiki/Elastic_collision
        # to find the new normal velocities (a 1D collision)
        new_v1n = (v1n * (e1.m - e2.m) + 2 * e2.m * v2n) / (e1.m + e2.m)
        new_v2n = (v2n * (e2.m - e1.m) + 2 * e1.m * v1n) / (e1.m + e2.m)

        # Calculate new velocities
        e1.v = new_v1n * unit_norm + v1t * unit_tang
        e2.v = new_v2n * unit_norm + v2t * unit_tang

        return e1, e2

    def _collision_handle(self, y):
        y = _extract_from_y_1d(y)
        e1_index, e2_index = _smallest_altitude_event(0, y, return_pair=True)
        e1 = self._physics_entity_at(y, e1_index)
        e2 = self._physics_entity_at(y, e2_index)
        e1, e2 = self._resolve_collision(e1, e2)
        log.info(f'Collision between {e1.name} and {e2.name}')
        y = self._merge_physics_entity_into(e1, y, e1_index)
        y = self._merge_physics_entity_into(e2, y, e2_index)
        return y

    def _derive(self, t, y_1d):
        X, Y, DX, DY = _extract_from_y_1d(y_1d)
        Xa, Ya = _get_acc(X, Y, self.M + self.Fuel)
        DX = DX + Xa
        DY = DY + Ya
        # We got a 1d row vector, make sure to return a 1d row vector.
        return np.concatenate((DX, DY, Xa, Ya), axis=None)

    def _state_from_y(self, t, y):
        y = _extract_from_y_1d(y)
        state = protos.PhysicalState()
        state.MergeFrom(self._template_physical_state)
        state.timestamp = t
        for x, y, vx, vy, entity in zip(
                y[0], y[1], y[2], y[3], state.entities):
            entity.x = x
            entity.y = y
            entity.vx = vx
            entity.vy = vy
        return state

    def get_state(self, requested_t=None):
        """Return the latest physical state of the simulation."""
        if requested_t is None:
            # Always update the current timestamp of our physical state.
            time_elapsed = self._time_elapsed()
            requested_t = self._template_physical_state.timestamp + \
                time_elapsed * self._time_acceleration

        while len(self._solutions) == 0 or \
                self._solutions[-1].t_max < requested_t:
            self._generate_new_ode_solutions(requested_t)

        for soln in self._solutions:
            if soln.t_min <= requested_t and requested_t <= soln.t_max:
                return self._state_from_y(
                    requested_t, soln(requested_t))
        log.error((
            'AAAAAAAAAAAAAAAAAH got an oopsy-woopsy! Tell your code monkey!'
            f'{self._solutions[0].t_min}, {self._solutions[-1].t_max}, '
            f'{requested_t}'
        ))
        breakpoint()

    def _generate_new_ode_solutions(self, requested_t):
        # An overview of how time is managed:
        # self._template_physical_state.timestamp is the current time in the
        # simulation. Every call to get_state(), it is incremented by the
        # amount of time that passed since the last call to get_state(),
        # factoring in time_acceleration.
        # self._solutions is a fixed-size queue of ODE solutions.
        # Each element has an attribute, t_max, which describes the largest
        # time that the solution can be evaluated at and still be accurate.
        # The highest such t_max should always be larger than the current
        # simulation time, i.e. self._template_physical_state.timestamp.
        latest_y = (self.X, self.Y, self.DX, self.DY)
        latest_t = self._solutions[-1].t_max if \
            len(self._solutions) else \
            self._template_physical_state.timestamp
        assert requested_t >= latest_t  # requested_t must be in the future

        while True:
            # self._solutions contains ODE solutions for the interval
            # [self._solutions[0].t_min, self._solutions[-1].t_max]
            # If we're in this function, requested_t is not in this interval!
            # Then we should integrate the interval of
            # [self._solutions[-1].t_max, requested_t]
            # and hopefully a bit farther past the end of that interval.
            ivp_out = scipy.integrate.solve_ivp(
                self._derive,
                [latest_t,
                 requested_t + FUTURE_WORK_SIZE * self._time_acceleration],
                # solve_ivp requires a 1D y0 array
                np.concatenate(latest_y, axis=None),
                events=self._events_function,
                max_step=MAX_STEP_SIZE,
                dense_output=True
            )
            
            self._solutions.append(ivp_out.sol)
            latest_y = _extract_from_y_1d(ivp_out.y[:, -1])
            latest_t = ivp_out.t[-1]

            if ivp_out.status < 0:
                # Integration error
                breakpoint()
            if ivp_out.status > 0:
                # We got a collision, simulation ends with the first collision.
                assert len(ivp_out.t_events[0]) == 1
                # The last column of the solution is the state at the collision
                latest_y = self._collision_handle(latest_y)
                # Redo the solve_ivp step
                continue
            elif latest_t >= requested_t:
                # Finished the requested amount of integration, now return
                break

        self.X, self.Y, self.DX, self.DY = latest_y
        self._template_physical_state.timestamp = latest_t


## These _functions are internal helper functions.
def _force(MM, X, Y):
    G = 6.674e-11
    D2 = np.square(X - X.transpose()) + np.square(Y - Y.transpose())
    # Calculate G * m1*m2/d^2 for each object pair.
    # In the diagonal case, i.e. an object paired with itself, force = 0.
    force_matrix = np.divide(MM, np.where(D2 != 0, D2, 1))
    np.fill_diagonal(force_matrix, 0)
    return G * force_matrix


def _force_sum(_force):
    return np.sum(_force, 0)


def _angle_matrix(X, Y):
    Xd = X - X.transpose()
    Yd = Y - Y.transpose()
    return np.arctan2(Yd, Xd)


def _polar_to_cartesian(ang, hyp):
    X = np.multiply(np.cos(ang), hyp)
    Y = np.multiply(np.sin(ang), hyp)
    return X.T, Y.T


def _f_to_a(f, M):
    return np.divide(f, M)


def _get_acc(X, Y, M):
    # Turn X, Y, M into column vectors, which is easier to do math with.
    # (row vectors won't transpose)
    X = X.reshape(1, -1)
    Y = Y.reshape(1, -1)
    M = M.reshape(1, -1)
    MM = np.outer(M, M)  # A square matrix of masses, units of kg^2
    ang = _angle_matrix(X, Y)
    FORCE = _force(MM, X, Y)
    Xf, Yf = _polar_to_cartesian(ang, FORCE)
    Xf = _force_sum(Xf)
    Yf = _force_sum(Yf)
    Xa = _f_to_a(Xf, M)
    Ya = _f_to_a(Yf, M)
    return np.array(Xa).reshape(-1), np.array(Ya).reshape(-1)

def _extract_from_y_1d(y_1d):
    # Split into 4 equal parts, [X, Y, DX, DY]
    if isinstance(y_1d, np.ndarray) and len(y_1d.shape) == 1:
        # If y is a 1D ODE input
        return np.hsplit(y_1d, 4)
    else:
        # If y is already 4 columns
        return y_1d

def _smallest_altitude_event(_, y_1d, return_pair=False):
    """This function is passed in to solve_ivp to detect a collision.

    To accomplish this, and also to stop solve_ivp when there is a collision,
    this function has the attributes `terminal = True` and `radii = [...]`.
    radii will be set by a PEngine when it's instantiated.
    Unfortunately this can't be a PEngine method, since attributes of a method
    can't be set.
    Ctrl-F 'events' in
    docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.solve_ivp.html
    for more info.
    This should return a scalar, and specifically 0 to indicate a collision
    """
    X, Y, DX, DY = _extract_from_y_1d(y_1d)
    n = len(X)
    posns = np.column_stack((X, Y))  # An n*2 vector of (x, y) positions
    # An n*n matrix of _altitudes_ between each entity
    alt_matrix = scipy.spatial.distance.cdist(posns, posns) - \
        (_smallest_altitude_event.radii + _smallest_altitude_event.radii.T)
    # To simplify calculations, an entity's altitude from itself is inf
    np.fill_diagonal(alt_matrix, np.inf)

    # Calculate approaching_matrix, where the i,j entry is -1 if entities i,j
    # are approaching each other.
    vx_col_vec = DX.reshape(1, -1)
    vy_col_vec = DY.reshape(1, -1)
    vx_sign_matrix = np.sign(vx_col_vec - vx_col_vec.T)
    vy_sign_matrix = np.sign(vy_col_vec - vy_col_vec.T)
    approaching_matrix = np.minimum(vx_sign_matrix, vy_sign_matrix)
    # To dodge inf*0=NaN errors, an entity's sign velocity from itself is 1
    np.fill_diagonal(approaching_matrix, 1)

    # n*n matrix of squared relative *altitudes*, each of which is negative if
    # the respective entities are approaching each other. Velocities of 0 are
    # not multiplied in.
    event_matrix = np.where(
        approaching_matrix,
        alt_matrix * approaching_matrix,
        alt_matrix  # If approaching_matrix[i][j] == 0, use this value.
    )
    # Return this 'signed altitude' of the two entities closest to colliding.
    if return_pair:
        # If we want to find out which entities collided, set return_pair=True.
        flattened_index = np.abs(event_matrix).argmin()
        # flattened_index is a value in the interval [1, n*n]-1. Turn it into a
        # 2D index.
        object_i = flattened_index // n
        object_j = flattened_index % n
        return object_i, object_j
    else:
        # This is how solve_ivp will invoke this function. Return a scalar.
        return event_matrix.flat[np.abs(event_matrix).argmin()]


_smallest_altitude_event.terminal = True  # Event stops integration
_smallest_altitude_event.direction = -1  # Event matters when going pos -> neg
_smallest_altitude_event.radii = []
