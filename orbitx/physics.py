import collections
import logging
import threading
import time
import warnings

import numpy as np
import scipy.integrate
import scipy.spatial
import scipy.special


from .physic_functions import *
from . import orbitx_pb2 as protos
from . import common
from .PhysicEntity import PhysicsEntity, Habitat

default_dtype = np.longdouble

SOLUTION_CACHE_SIZE = 5


NOT_BROKEN = 0
warnings.simplefilter('error')  # Raise exception on numpy RuntimeWarning
scipy.special.seterr(all='raise')
log = logging.getLogger()

# Note on variable naming:
# a lowercase single letter, like `x`, is likely a scalar
# an uppercase single letter, like `X`, is likely a 1D row vector of scalars
# `y` is either the y position, or a vector of the form [X, Y, DX, DY]. i.e. 2D
# `y_1d` specifically is the 1D row vector flattened version of the above `y`
#     2D y vector, which is required by `solve_ivp` inputs and outputs

# Note on the np.array family of functions:
# https://stackoverflow.com/a/52103839/1333978
# Basically, it can sometimes be important in this module whether a call to
# np.array() is copying something, or changing the dtype, etc.

# Note on where to add new variables to the simulation:
#
# If you're adding something that will never change over the course of the
# simulation, you should add that variable to the .proto definition of an
# entity (and it would be best to optionally add it to PhysicsEntity as well).
# Examples of these kinds of variables are name and mass. Then, when a
# physical_state is loaded by set_state, these immutable variables will be
# stored in self._template_physical_state, no extra action needed.
#
# If you're adding something that will change over the course of a simulation,
# you should add that variable to the Y class, which represents the state
# of mutable variables in the simulation at a certain point in time. For
# example, position, velocity, fuel, and throttle are all mutable variables
# that go in the Y class. Look for the string "# PROTO:" for where these
# kinds of variables should be inserted, since there's some bookkeeping needed
# to translate these variables from the internal representation (a bunch of
# continuously-evaluable functions of time that return a numpy.ndarray) to
# a physical_state that our API users expect.


class Y():
    """Wraps a y0 input/output to solve_ivp.
    y0 = [X, Y, VX, VY, Heading, Spin, Fuel, Throttle, AttachedTo, Broken].

    Example usage:
    y = Y(physical_state, or y_1d)
    y.X[5] = 32.2
    print(y.Fuel[4])
    """

    def __init__(self, data):
        """Takes either an np.ndarray or a PhysicalState."""
        if isinstance(data, np.ndarray):
            assert len(data.shape) == 1
            self._y_1d = data
        else:
            # PROTO: if you're changing protobufs remember to change here
            # and also make an accessor for your variable
            assert isinstance(data, protos.PhysicalState)
            X = np.array([entity.x for entity in data.entities])
            Y = np.array([entity.y for entity in data.entities])
            VX = np.array([entity.vx for entity in data.entities])
            VY = np.array([entity.vy for entity in data.entities])
            Heading = np.array([entity.heading for entity in data.entities])
            Spin = np.array([entity.spin for entity in data.entities])
            Fuel = np.array([entity.fuel for entity in data.entities])
            Throttle = np.array([entity.throttle for entity in data.entities])

            # Internally translate string names to indices, otherwise
            # our entire y vector will turn into a string vector oh no.
            # Note this will be converted to floats, not integer indices.
            AttachedTo = np.array([
                name_to_index(entity.attached_to, data.entities)
                for entity in data.entities
            ])

            Broken = np.array([entity.broken for entity in data.entities])

            self._y_1d = np.concatenate(
                (
                    X, Y, VX, VY, Heading, Spin,
                    Fuel, Throttle, AttachedTo, Broken
                ), axis=None).astype(default_dtype)

        # y_1d has 10 components
        self._n = len(self._y_1d) // 10

        assert min(self.AttachedTo) >= NO_INDEX
        assert max(self.AttachedTo) < self._n

    @property
    def X(self):
        return self._y_1d[0 * self._n:1 * self._n]

    @property
    def Y(self):
        return self._y_1d[1 * self._n:2 * self._n]

    @property
    def VX(self):
        return self._y_1d[2 * self._n:3 * self._n]

    @property
    def VY(self):
        return self._y_1d[3 * self._n:4 * self._n]

    @property
    def Heading(self):
        return self._y_1d[4 * self._n:5 * self._n]

    def BoundHeading(self):
        """Coerce internal heading values to [0, 2pi)."""
        self._y_1d[4 * self._n:5 * self._n] %= (2 * np.pi)

    @property
    def Spin(self):
        return self._y_1d[5 * self._n:6 * self._n]

    @property
    def Fuel(self):
        return self._y_1d[6 * self._n:7 * self._n]

    @property
    def Throttle(self):
        return self._y_1d[7 * self._n:8 * self._n]

    @property
    def AttachedTo(self):
        return self._y_1d[8 * self._n:9 * self._n]

    @property
    def Broken(self):
        return self._y_1d[9 * self._n:10 * self._n]


class PEngine(object):
    """Physics Engine class. Encapsulates simulating physical state.

    Methods beginning with an underscore are not part of the API and change!

    Example usage:
    pe = PEngine(flight_savefile)
    state = pe.get_state()
    pe.set_time_acceleration(20)
    # Simulates 20 * [amount of time elapsed since last get_state() call]:
    state = pe.get_state()

    This class will start a background thread to simulate physics when __init__
    is called. This background thread may restart at arbitrary times.
    This class is designed to be access from the main thread by methods that
    don't begin with an underscore, so thread synchronization between the main
    thread and the background solutions thread is done with this assumption in
    mind. If this assumption changes, change thread synchronization code very
    deliberately and carefully! Specifically, if you're in spacesim, feel free
    to hit me (Patrick) up, and I can help.
    """

    def __init__(self, physical_state):
        # A PhysicalState, but no x, y, vx, or vy. That's handled by get_state.
        self._template_physical_state = protos.PhysicalState()

        # Controls access to self._solutions. If anything changes that is
        # related to self._solutions, this condition variable should be
        # notified. Currently, that's just if self._solutions or
        # self._last_simtime changes.
        self._solutions_cond = threading.Condition()
        self._last_monotime = time.monotonic()
        self._time_acceleration = common.DEFAULT_TIME_ACC
        self._simthread_exception = None

        self.set_state(physical_state)

    def set_time_acceleration(self, time_acceleration, requested_t=None):
        """Change the speed at which this PEngine simulates at."""
        self.handle_command(protos.Command(
            ident=protos.Command.TIME_ACC_SET, arg=time_acceleration))

    def _simtime(self, requested_t=None, *, peek_time=False):
        """Gets simulation time, accounting for time acceleration and passage.

        peek_time: if True, doesn't change any internal state. Use this as True
                   for internal log messages, and False for external APIs.
        """
        # During runtime, strange things will happen if you mix calling
        # this with None (like in flight.py) or with values (like in test.py)

        if requested_t is None:
            time_elapsed = max(
                time.monotonic() - self._last_monotime,
                0.0001
            )
            if not peek_time:
                self._last_monotime = time.monotonic()
            requested_t = (
                self._last_simtime + time_elapsed * self._time_acceleration)

        if not peek_time:
            with self._solutions_cond:
                self._last_simtime = requested_t
                self._solutions_cond.notify_all()

        return requested_t

    def _stop_simthread(self):
        if hasattr(self, '_simthread'):
            with self._solutions_cond:
                self._stopping_simthread = True
                self._solutions_cond.notify_all()
            self._simthread.join()

    def _restart_simulation(self, t0, y0):
        self._stop_simthread()

        self._simthread = threading.Thread(
            target=self._simthread_target,
            args=(t0, y0),
            name='simthread',
            daemon=True
        )
        self._stopping_simthread = False

        # We don't need to synchronize self._last_simtime or
        # self._solutions here, because we just stopped the background
        # simulation thread only a few lines ago.
        self._last_simtime = t0

        # Essentially just a cache of ODE solutions.
        self._solutions = collections.deque(maxlen=SOLUTION_CACHE_SIZE)

        # Fork self._simthread into the background.
        self._simthread.start()

    def _physics_entity_at(self, y, i):
        """Returns a PhysicsEntity constructed from the i'th entity."""
        # PROTO: if you're changing protobufs remember to change here
        protobuf_entity = self._template_physical_state.entities[i]
        protobuf_entity.x = y.X[i]
        protobuf_entity.y = y.Y[i]
        protobuf_entity.vx = y.VX[i]
        protobuf_entity.vy = y.VY[i]
        protobuf_entity.heading = y.Heading[i]
        protobuf_entity.spin = y.Spin[i]
        protobuf_entity.fuel = y.Fuel[i]
        protobuf_entity.throttle = y.Throttle[i]

        physics_entity = PhysicsEntity(protobuf_entity)
        physics_entity.attached_to = index_to_name(
            y.AttachedTo[i], self._template_physical_state.entities)
        physics_entity.broken = bool(int(y.Broken[i]))
        return physics_entity

    def _merge_physics_entity_into(self, physics_entity, y, i):
        """Inverse of _physics_entity_at, merges a physics_entity into y."""
        # PROTO: if you're changing protobufs remember to change here
        y.X[i], y.Y[i] = physics_entity.pos
        y.VX[i], y.VY[i] = physics_entity.v
        y.Heading[i] = physics_entity.heading
        y.Spin[i] = physics_entity.spin
        y.Fuel[i] = physics_entity.fuel
        y.Throttle[i] = physics_entity.throttle
        y.AttachedTo[i] = name_to_index(
            physics_entity.attached_to, self._template_physical_state.entities)
        y.Broken[i] = physics_entity.broken
        return y
    def set_control_craft_index(self,index):
        self.control_craft_index=index
    def handle_command(self, command, requested_t=None):
        """Interface to set habitat controls.

        Use an argument to change habitat throttle or spinning, and simulation
        will restart with this new information."""
        requested_t = self._simtime(requested_t)
        y0 = Y(self.get_state(requested_t))
        log.info(f'Got command for simtime t={requested_t}: {command}')

        # Make sure we've slowed down, stuff is about to happen.
        self._time_acceleration = common.DEFAULT_TIME_ACC

        # temp change before implementatin of spacestation
        control_craft_index = self._hab_index

        def launch():
            nonlocal y0
            # Make sure that we're slightly above the surface of an attachee
            # before un-attaching two entities
            attachee_index = int(y0.AttachedTo[control_craft_index])
            if attachee_index != NO_INDEX:
                ship = self._physics_entity_at(y0, control_craft_index)
                attachee = self._physics_entity_at(y0, attachee_index)

                norm = ship.pos - attachee.pos
                unit_norm = norm / np.linalg.norm(norm)
                # This magic number is anything that is small to make much of
                # an effect, but positive so that a collision will be detected
                ship.pos = attachee.pos + unit_norm * (
                    common.LAUNCH_SEPARATION + attachee.r + ship.r)

                y0 = self._merge_physics_entity_into(
                    ship, y0, control_craft_index)
                y0.AttachedTo[control_craft_index] = NO_INDEX

        if command.ident == protos.Command.HAB_SPIN_CHANGE:
            if y0.AttachedTo[control_craft_index] == NO_INDEX:
                y0.Spin[control_craft_index] += Habitat.spin_change(
                    requested_spin_change=command.arg)
        elif command.ident == protos.Command.HAB_THROTTLE_CHANGE:
            launch()
            y0.Throttle[control_craft_index] += command.arg
        elif command.ident == protos.Command.HAB_THROTTLE_SET:
            launch()
            y0.Throttle[control_craft_index] = command.arg

        elif command.ident == protos.Command.TIME_ACC_SET:
            assert command.arg > 0
            self._time_acceleration = command.arg

        # Bound throttle to [-20, 120] percent
        y0.Throttle[control_craft_index] = max(
            common.MIN_THROTTLE, y0.Throttle[control_craft_index])
        y0.Throttle[control_craft_index] = min(
            common.MAX_THROTTLE, y0.Throttle[control_craft_index])

        # Have to restart simulation when any controls are changed
        self._restart_simulation(requested_t, y0)

    def set_state(self, physical_state):
        self._artificials=np.where(np.array([entity.artificial for entity in physical_state.entities]) >= 1)[0]
        try:
            self._hab_index = [
                entity.name for entity in physical_state.entities
            ].index('Habitat')
        except ValueError:
            self._hab_index = 0

        try:
            self._spacestation_index = [
                entity.name for entity in physical_state.entities
            ].index('SpaceStation')
        except ValueError:
            self._spacestation_index = 0

        y0 = Y(physical_state)
        self.R = np.array([entity.r for entity in physical_state.entities]
                          ).astype(default_dtype)
        self.M = np.array([entity.mass for entity in physical_state.entities]
                          ).astype(default_dtype)

        # Don't store variables that belong in y0 in the physical_state,
        # it should only be returned from get_state()
        #protentially problematic ??
        self._template_physical_state.CopyFrom(physical_state)
        for entity in self._template_physical_state.entities:
            # PROTO: if you're changing protobufs remember to change here
            entity.ClearField('x')
            entity.ClearField('y')
            entity.ClearField('vx')
            entity.ClearField('vy')
            entity.ClearField('heading')
            entity.ClearField('spin')
            entity.ClearField('fuel')
            entity.ClearField('throttle')
            if hasattr(entity, "attached_to"):
                entity.ClearField('attached_to')
            if hasattr(entity, "attached_to"):
                entity.ClearField('broken')

        self._restart_simulation(physical_state.timestamp, y0)

    def _collision_decision(self, t, y, altitude_event):
        e1_index, e2_index = altitude_event(
            t, y._y_1d, return_pair=True)
        e1 = self._physics_entity_at(y, e1_index)
        e2 = self._physics_entity_at(y, e2_index)

        log.info(f'Collision {t - self._simtime(peek_time=True)} '
                 f'seconds in the future, {e1.as_proto()} and {e2.as_proto()}')

        if (e2.attached_to is not "") or (e1.attached_to is not ""):
            log.info('Entities are attached, returning early')
            return e1, e2

        if e1.artificial:
            self._land(e1, e2)
        elif e2.artificial:
            self._land(e2, e1)
        else:
            self._bounce(e1, e2)

        y = self._merge_physics_entity_into(e1, y, e1_index)
        y = self._merge_physics_entity_into(e2, y, e2_index)
        return y

    def _bounce(self, e1, e2):
        # Resolve a collision by:
        # 1. calculating positions and velocities of the two entities
        # 2. do a 1D collision calculation along the normal between the two
        # 3. recombine the velocity vectors
        log.info(f'Bouncing {e1.name} and {e2.name}')
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

    def _land(self, e1, e2):
        # e1 is an artificial object
        # if 2 artificial object collide (habitat, spacespation)
        # or small astroid collision (need deletion), handle later
        log.info(f'Landing {e1.name} on {e2.name}')
        assert not e2.artificial
        assert not e2.cannot_land
        e1.attached_to = e2.name

        # Currently does nothing
        e1.broken = np.linalg.norm(e1.v - e2.v) > e1.habitat_hull_strength

        # set right heading for future takeoff
        norm = e1.pos - e2.pos
        e1.heading = np.arctan2(norm[1], norm[0])
        e1.throttle = 0
        e1.spin = e2.spin
        e1.v = e2.v

    def _state_from_y_1d(self, t, y):
        y = Y(y)
        state = protos.PhysicalState()
        state.MergeFrom(self._template_physical_state)
        state.timestamp = t
        entity_names = [
            entity.name for entity in self._template_physical_state.entities
        ]
        # PROTO: if you're changing protobufs remember to change here
        for x, y, vx, vy, heading, spin, fuel, throttle, \
            attached_index, broken, entity in zip(
                y.X, y.Y, y.VX, y.VY, y.Heading, y.Spin,
                y.Fuel, y.Throttle, y.AttachedTo, y.Broken,
                state.entities):
            entity.x = x
            entity.y = y
            entity.vx = vx
            entity.vy = vy
            entity.heading = heading
            entity.spin = spin
            entity.fuel = fuel
            entity.throttle = throttle
            # We don't use index_to_name because we're doing a whole list at
            # once, not just a single translation. So we optimize for that.
            attached_index = int(attached_index)

            # (no field "broken" in protocol message object).
            if attached_index != NO_INDEX:
                entity.attached_to = entity_names[attached_index]
            if broken != NOT_BROKEN:
                entity.broken = bool(int(broken))
        return state

    def get_state(self, requested_t=None):
        """Return the latest physical state of the simulation."""
        requested_t = self._simtime(requested_t)

        # Wait until there is a solution for our requested_t. The .wait_for()
        # call will block until a new ODE solution is created.
        with self._solutions_cond:
            self._last_simtime = requested_t
            self._solutions_cond.wait_for(
                lambda:
                len(self._solutions) != 0 and
                self._solutions[-1].t_max >= requested_t or
                self._simthread_exception is not None
            )

            # Check if the simthread crashed
            if self._simthread_exception is not None:
                raise self._simthread_exception

            # We can't integrate backwards, so if integration has gone
            # beyond what we need, fail early.
            assert requested_t >= self._solutions[0].t_min

            for soln in self._solutions:
                if soln.t_min <= requested_t and requested_t <= soln.t_max:
                    solution = soln

        return self._state_from_y_1d(requested_t, solution(requested_t))

    def _simthread_target(self, t, y):
        log.debug('simthread started')
        try:
            self._generate_new_ode_solutions(t, y)
        except Exception as e:
            log.exception('simthread got exception, forwarding to main thread')
            self._simthread_exception = e
        log.debug('simthread exited')

    def _derive(self, t, y_1d):
        """
        y_1d =
         [X, Y, VX, VY, Heading, Spin, Fuel, Throttle, AttachedTo, Broken]
        returns the derivative of y_1d, i.e.
        [VX, VY, AX, AY, Spin, 0, Fuel consumption, 0, 0, 0]
        (zeroed-out fields are changed elsewhere)

        !!!!!!!!!!! IMPORTANT !!!!!!!!!!!
        This function should return a DERIVATIVE. The numpy.solve_ivp function
        will do the rest of the work of the simulation, this function just
        describes how things _move_.
        At its most basic level, this function takes in the _position_ of
        everything (plus some random stuff), and returns the _velocity_ of
        everything (plus some random stuff).
        Essentially, numpy.solve_ivp does this calculation:
        new_positions_of_system = t_delta * _derive(
                                                current_t_of_system,
                                                current_y_of_system)
        """
        # PROTO: if you're changing protobufs remember to change here
        y = Y(y_1d)
        Xa, Ya = grav_acc(y.X, y.Y, self.M + y.Fuel)
        zeros = np.zeros(y._n)
        fuel_cons = np.zeros(y._n)

        for index in self._artificials:
            if y.Fuel[index] > 0:
                # We have fuel remaining, calculate thrust
                throttle = y.Throttle[index]
                fuel_cons[index] = - \
                    Habitat.fuel_cons(throttle=throttle)
                hab_eng_acc_x, hab_eng_acc_y = Habitat.acceleration(
                    throttle=throttle, heading=y.Heading[index])
                Xa[index] += hab_eng_acc_x
                Ya[index] += hab_eng_acc_y

        # Keep attached entities glued together
        for attached, attachee in enumerate(y.AttachedTo):
            # The entity at index 'attached' is attached to the entity at index
            # 'attachee'. For example, the habitat could be attached to the
            # Earth, in which case 'attached' would reference the habitat and
            # 'attachee' would reference the Earth
            if attachee == NO_INDEX:
                continue
            attachee = int(attachee)

            # If we're attached to something, make sure we move in lockstep.
            y.VX[attached] = y.VX[attachee]
            y.VY[attached] = y.VY[attachee]
            Xa[attached] = Xa[attachee]
            Ya[attached] = Ya[attachee]

            # We also add velocity and centripetal acceleration that comes
            # from being attached to the surface of a spinning object.
            e1 = self._physics_entity_at(y, attached)
            e2 = self._physics_entity_at(y, attachee)
            norm = e1.pos - e2.pos
            unit_norm = norm / np.linalg.norm(norm)
            # The unit tangent is perpendicular to the unit normal vector
            unit_tang = np.asarray([-unit_norm[1], unit_norm[0]])
            # These two lines courtesy of wikipedia "Circular motion"
            circular_velocity = unit_tang * e2.spin * e2.r
            centripetal_acc = unit_norm * e2.spin ** 2 * e2.r
            y.VX[attached] += circular_velocity[0]
            y.VY[attached] += circular_velocity[1]
            Xa[attached] += centripetal_acc[0]
            Ya[attached] += centripetal_acc[1]

        # for futur spacestation code
        # if y.Fuel[self._spacestation_index] > 0:
        #    # We have fuel remaining, calculate thrust
        #    throttle = y.Throttle[self._spacestation_index]
        #    fuel_cons[self._spacestation_index] = \
        #        -Habitat.fuel_cons(throttle=throttle)
        #    hab_eng_acc_x, hab_eng_acc_y = Habitat.acceleration(
        #        throttle=throttle,
        #        heading=y.Heading[self._spacestation_index])
        #    Xa[self._spacestation_index] += hab_eng_acc_x
        #    Ya[self._spacestation_index] += hab_eng_acc_y

        return np.concatenate(
            (
                y.VX, y.VY, Xa, Ya, y.Spin, zeros,
                fuel_cons, zeros, zeros, zeros
            ), axis=None)

    def _generate_new_ode_solutions(self, t, y):
        # An overview of how time is managed:
        #
        # self._last_simtime is the main thread's latest idea of
        # what the current time is in the simulation. Every call to
        # get_state(), self._timetime_of_last_request is incremented by the
        # amount of time that passed since the last call to get_state(),
        # factoring in self._time_acceleration.
        #
        # self._solutions is a fixed-size queue of ODE solutions.
        # Each element has an attribute, t_max, which describes the largest
        # time that the solution can be evaluated at and still be accurate.
        # The highest such t_max should always be larger than the current
        # simulation time, i.e. self._last_simtime

        def hab_fuel_event(t, y_1d):
            """Return a 0 only when throttle is nonzero."""
            y = Y(y_1d)
            for index in self._artificials:
                if y.Throttle[index] != 0:
                    return Y(y_1d).Fuel[index]
            else:
                return 1
        hab_fuel_event.terminal = True
        hab_fuel_event.direction = -1

        def altitude_event(_, y_1d, return_pair=False):
            """An event function. See numpy documentation for solve_ivp.

            @return a scalar, with 0 indicating a collision and a sign change
                indicating a collision has happened.
            """
            y = Y(y_1d)
            n = y._n
            posns = np.column_stack((y.X, y.Y))  # 2xN of (x, y) positions
            # An n*n matrix of _altitudes_ between each entity
            alt_matrix = (
                scipy.spatial.distance.cdist(posns, posns) -
                np.array([self.R]) - np.array([self.R]).T)
            # To simplify calculations, an entity's altitude from itself is inf
            np.fill_diagonal(alt_matrix, np.inf)
            # For each pair of objects that have collisions disabled between
            # them, also set their altitude to be inf

            # If there are any entities attached to any other entities, ignore
            # both the attachee and the attached entity.
            for index in np.where(y.AttachedTo >= 0)[0]:
                alt_matrix[index, int(y.AttachedTo[index])] = np.inf
                alt_matrix[int(y.AttachedTo[index]), index] = np.inf

            if return_pair:
                # Returns the actual pair of indicies instead of a scalar.
                flattened_index = alt_matrix.argmin()
                # flattened_index is a value in the interval [1, n*n]-1.
                # Turn it into a  2D index.
                object_i = flattened_index // n
                object_j = flattened_index % n
                return object_i, object_j
            else:
                # solve_ivp invocation, return scalar
                return np.min(alt_matrix)

        altitude_event.terminal = True  # Event stops integration
        altitude_event.direction = -1  # Event matters when going pos -> neg

        while not self._stopping_simthread:
            # self._solutions contains ODE solutions for the interval
            # [self._solutions[0].t_min, self._solutions[-1].t_max]
            # If we're in this function, requested_t is not in this interval!
            # Then we should integrate the interval of
            # [self._solutions[-1].t_max, requested_t]
            # and hopefully a bit farther past the end of that interval.
            y.BoundHeading()
            ivp_out = scipy.integrate.solve_ivp(
                self._derive,
                [t, t + self._time_acceleration],
                # solve_ivp requires a 1D y0 array
                y._y_1d,
                events=[altitude_event, hab_fuel_event],
                # np.sqrt is here to give a slower-than-linear step size growth
                max_step=np.sqrt(self._time_acceleration),
                dense_output=True
            )

            if ivp_out.status < 0:
                # Integration error
                log.warning(ivp_out.message)
                log.warning(('Retrying with decreased time acceleration = '
                             f'{self._time_acceleration / 10}'))
                self._time_acceleration = self._time_acceleration / 10
                if self._time_acceleration < 1:
                    # We can't lower the time acceleration anymore
                    raise Exception(ivp_out.message)
                continue

            # When we create a new solution, let other people know.
            with self._solutions_cond:
                # If adding another solution to our max-sized deque would drop
                # our oldest solution, and the main thread is still asking for
                # state in the t interval of our oldest solution, take a break
                # until the main thread has caught up.
                self._solutions_cond.wait_for(
                    lambda:
                    len(self._solutions) < SOLUTION_CACHE_SIZE or
                    self._last_simtime > self._solutions[0].t_max or
                    self._stopping_simthread
                )
                if self._stopping_simthread:
                    break

                self._solutions.append(ivp_out.sol)
                self._solutions_cond.notify_all()

            y = Y(ivp_out.y[:, -1])
            t = ivp_out.t[-1]

            if ivp_out.status > 0:
                log.debug(f'Got event: {ivp_out.t_events}')
                if len(ivp_out.t_events[0]):
                    # Collision, simulation ended. Handled it and continue.
                    assert len(ivp_out.t_events[0]) == 1
                    assert len(ivp_out.t) >= 2
                    y = self._collision_decision(t, y, altitude_event)
                elif len(ivp_out.t_events[1]):
                    log.debug(f'Got fuel empty at {t}')
                    
                    for index in self._artificials:
                        # Habitat's out of fuel, the next iteration won't consume
                        # any fuel. Set throttle to zero anyway.
                        y.Throttle[index] = 0
                        # Set fuel to a negative value, so it doesn't trigger
                        # the event function
                        y.Fuel[index] = 0

