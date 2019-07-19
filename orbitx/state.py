"""Classes that represent the state of the entire system and entities within.

These classes wrap protobufs, which are basically a fancy NamedTuple that is
generated by the `build` Makefile target. You can read more about protobufs
online, but mainly they're helpful for serializing data over the network."""

import logging
from enum import Enum
from typing import List, Dict, Optional, Union

import numpy as np
import vpython

from orbitx import orbitx_pb2 as protos
from orbitx import common

log = logging.getLogger()


# Make sure this is in sync with the corresponding enum in orbitx.proto!
Navmode = Enum('Navmode', zip([  # type: ignore
    'Manual', 'CCW Prograde', 'CW Retrograde', 'Depart Reference',
    'Approach Target', 'Pro Targ Velocity', 'Anti Targ Velocity'
], protos.Navmode.values()))


class Entity:
    """A wrapper around protos.Entity.

    Example usage:
    assert Entity(protos.Entity(x=5)).x == 5
    assert Entity(protos.Entity(x=1, y=2)).pos == [1, 2]

    To add fields, or see what fields exists, please see orbitx.proto,
    specifically the "message Entity" declaration.
    """
    habitat_hull_strength = 50
    spacestation_hull_strength = 100

    def __init__(self, entity: protos.Entity):
        assert isinstance(entity, protos.Entity)
        self.proto = entity

    def __repr__(self):
        return self.proto.__repr__()

    def __str__(self):
        return self.proto.__str__()

    # These are filled in just below this class definition. These stubs are for
    # static type analysis with mypy.
    name: str
    x: float
    y: float
    vx: float
    vy: float
    r: float
    mass: float
    heading: float
    spin: float
    fuel: float
    throttle: float
    landed_on: str
    broken: bool
    artificial: bool
    atmosphere_thickness: float
    atmosphere_scaling: float

    def screen_pos(self, origin: 'Entity') -> vpython.vector:
        """The on-screen position of this entity, relative to the origin."""
        return vpython.vector(self.x - origin.x, self.y - origin.y, 0)

    @property
    def pos(self):
        return np.asarray([self.proto.x, self.proto.y])

    @pos.setter
    def pos(self, x):
        self.proto.x = x[0]
        self.proto.y = x[1]

    @property
    def v(self):
        return np.asarray([self.proto.vx, self.proto.vy])

    @v.setter
    def v(self, x):
        self.proto.vx = x[0]
        self.proto.vy = x[1]

    # TODO: temporary solution to detect AYSE, need improvement
    @property
    def dockable(self):
        return self.name == common.AYSE

    def landed(self) -> bool:
        """Convenient and more elegant check to see if the entity is landed."""
        return self.landed_on != ''


for field in protos.Entity.DESCRIPTOR.fields:
    # For every field in the underlying protobuf entity, make a
    # convenient equivalent property to allow code like the following:
    # Entity(entity).heading = 5

    def fget(self, name=field.name):
        return getattr(self.proto, name)

    def fset(self, val, name=field.name):
        return setattr(self.proto, name, val)

    def fdel(self, name=field.name):
        return delattr(self.proto, name)

    # Assert that there is a stub for the field before setting it.
    setattr(Entity, field.name, property(
        fget=fget, fset=fset, fdel=fdel,
        doc=f"Proxy of the underlying field, self.proto.{field.name}"))


class PhysicsState:
    """The physical state of the system for use in solve_ivp and elsewhere.

    The following operations are supported:

    # Construction without a y-vector, taking all data from a PhysicalState
    PhysicsState(None, protos.PhysicalState)

    # Faster Construction from a y-vector and protos.PhysicalState
    PhysicsState(ivp_solution.y, protos.PhysicalState)

    # Access of a single Entity in the PhysicsState, by index or Entity name
    my_entity: Entity = PhysicsState[0]
    my_entity: Entity = PhysicsState['Earth']

    # Iteration over all Entitys in the PhysicsState
    for entity in my_physics_state:
        print(entity.name, entity.pos)

    # Convert back to a protos.PhysicalState (this almost never happens)
    my_physics_state.as_proto()

    Example usage:
    y = PhysicsState(physical_state, y_1d)

    entity = y[0]
    y[common.HABITAT] = habitat
    scipy.solve_ivp(y.y0())

    See help(PhysicsState.__init__) for how to initialize. Basically, the `y`
    param should be None at the very start of the program, but for the program
    to have good performance, PhysicsState.__init__ should have both parameters
    filled if it's being called more than once a second while OrbitX is running
    normally.
    """

    class NoEntityError(ValueError):
        """Raised when an entity is not found."""
        pass

    # For if an entity is not landed to anything
    NO_INDEX = -1

    # Number of different kinds of variables in the internal y vector. The
    # internal y vector has length N_COMPONENTS * len(proto_state.entities).
    # For example, if the y-vector contained just x, y, vx, and vy, then
    # N_COMPONENTS would be 4.
    N_COMPONENTS = 10

    # Datatype of internal y-vector
    DTYPE = np.longdouble

    def __init__(self,
                 y: Optional[np.ndarray],
                 proto_state: protos.PhysicalState):
        """Collects data from proto_state and y, when y is not None.

        There are two kinds of values we care about:
        1) values that change during simulation (like position, velocity, etc)
        2) values that do not change (like mass, radius, name, etc)

        If both proto_state and y are given, 1) is taken from y and
        2) is taken from proto_state. This is a very quick operation.

        If y is None, both 1) and 2) are taken from proto_state, and a new
        y vector is generated. This is a somewhat expensive operation."""
        assert isinstance(proto_state, protos.PhysicalState)
        assert isinstance(y, np.ndarray) or y is None

        # self._proto_state will have positions, velocities, etc for all
        # entities. DO NOT USE THESE they will be stale. Use the accessors of
        # this class instead!
        self._proto_state = protos.PhysicalState()
        self._proto_state.CopyFrom(proto_state)
        self._n = len(proto_state.entities)

        self._entity_names = \
            [entity.name for entity in self._proto_state.entities]

        if y is None:
            # PROTO: if you're changing protobufs remember to change here
            X = np.array([entity.x for entity in proto_state.entities])
            Y = np.array([entity.y for entity in proto_state.entities])
            VX = np.array([entity.vx for entity in proto_state.entities])
            VY = np.array([entity.vy for entity in proto_state.entities])
            Heading = np.array([
                entity.heading for entity in proto_state.entities])
            Spin = np.array([entity.spin for entity in proto_state.entities])
            Fuel = np.array([entity.fuel for entity in proto_state.entities])
            Throttle = np.array([
                entity.throttle for entity in proto_state.entities])
            np.clip(Throttle, common.MIN_THROTTLE, common.MAX_THROTTLE,
                    out=Throttle)

            # Internally translate string names to indices, otherwise
            # our entire y vector will turn into a string vector oh no.
            # Note this will be converted to floats, not integer indices.
            LandedOn = np.array([
                self._name_to_index(entity.landed_on)
                for entity in proto_state.entities
            ])

            Broken = np.array([
                entity.broken for entity in proto_state.entities
            ])

            self._y0: np.ndarray = np.concatenate((
                X, Y, VX, VY, Heading, Spin,
                Fuel, Throttle, LandedOn, Broken,
                np.array([self._proto_state.srb_time])),
                axis=0).astype(self.DTYPE)
        else:
            # Take everything except the SRB time, the last element.
            self._y0: np.ndarray = y.astype(self.DTYPE)
            self._proto_state.srb_time = y[-1]

        assert len(self._y0.shape) == 1, f'y is not 1D: {self._y0.shape()}'
        assert (self._y0.size - 1) % self.N_COMPONENTS == 0, self._y0.size
        assert (self._y0.size - 1) // self.N_COMPONENTS == \
            len(proto_state.entities), \
            f'{self._y0.size} != {len(proto_state.entities)}'
        self._n = (len(self._y0) - 1) // self.N_COMPONENTS

        self._entities_with_atmospheres: List[int] = []
        for index, entity in enumerate(self._proto_state.entities):
            if entity.atmosphere_scaling != 0 and \
                    entity.atmosphere_thickness != 0:
                self._entities_with_atmospheres.append(index)

    def _y_entities(self) -> np.ndarray:
        """Internal, returns an array for every entity, each with an element
        for each component."""
        return np.transpose(self._y_components())

    def _y_components(self) -> np.ndarray:
        """Internal, returns N_COMPONENT number of arrays, each with an element
        for each entity."""
        return self._y0[0:-1].reshape(self.N_COMPONENTS, -1)

    def _index_to_name(self, index: int) -> str:
        """Translates an index into the entity list to the right name."""
        i = int(index)
        return self._entity_names[i] if i != self.NO_INDEX else ''

    def _name_to_index(self, name: Optional[str]) -> int:
        """Finds the index of the entity with the given name."""
        try:
            return self._entity_names.index(name) if name != '' \
                else self.NO_INDEX
        except ValueError:
            raise self.NoEntityError(f'{name} not in entity list')

    def y0(self):
        """Returns a y-vector suitable as input for scipy.solve_ivp."""
        # Ensure that heading is within [0, 2pi).
        self._y_components()[4] %= (2 * np.pi)
        return self._y0

    def as_proto(self) -> protos.PhysicalState:
        """Creates a protos.PhysicalState view into all internal data.

        Expensive. Consider one of the other accessors, which are faster.
        For example, if you want to iterate over all elements, use __iter__
        by doing:
        for entity in my_physics_state: print(entity.name)"""
        constructed_protobuf = protos.PhysicalState()
        constructed_protobuf.CopyFrom(self._proto_state)
        for entity_data, entity in zip(
                self._y_entities(),
                constructed_protobuf.entities):

            entity.x, entity.y, entity.vx, entity.vy, entity.heading, \
                entity.spin, entity.fuel, entity.throttle, \
                landed_index, broken = entity_data

            entity.landed_on = self._index_to_name(landed_index)
            entity.broken = bool(broken)
        return constructed_protobuf

    def __len__(self):
        """Implements `len(physics_state)`."""
        return self._n

    def __iter__(self):
        """Implements `for entity in physics_state:` loops."""
        for i in range(0, self._n):
            yield self.__getitem__(i)

    def __getitem__(self, index: Union[str, int, None]) -> Entity:
        """Returns a Entity view at a given name or index.

        Allows the following:
        physics_entity = PhysicsState[2]
        physics_entity = PhysicsState[common.HABITAT]
        """
        assert index is not None
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        entity = self._proto_state.entities[i]

        entity.x, entity.y, entity.vx, entity.vy, entity.heading, \
            entity.spin, entity.fuel, entity.throttle, \
            landed_index, broken = \
            self._y_entities()[i]

        entity.landed_on = self._index_to_name(landed_index)
        entity.broken = bool(broken)
        return Entity(entity)

    def __setitem__(self, index: Union[str, int, None], val: Entity):
        """Puts a Entity at a given name or index in the state.

        Allows the following:
        PhysicsState[2] = physics_entity
        PhysicsState[common.HABITAT] = physics_entity
        """
        # TODO: allow y[common.HABITAT].fuel = 5
        assert index is not None
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        # Bound throttle
        val.throttle = max(common.MIN_THROTTLE, val.throttle)
        val.throttle = min(common.MAX_THROTTLE, val.throttle)

        landed_index = self._name_to_index(val.landed_on)

        self._y_entities()[i] = np.array([
            val.x, val.y, val.vx, val.vy, val.heading, val.spin, val.fuel,
            val.throttle, landed_index, val.broken
        ]).astype(self.DTYPE)

    @property
    def timestamp(self) -> float:
        return self._proto_state.timestamp

    @timestamp.setter
    def timestamp(self, t: float):
        self._proto_state.timestamp = t

    @property
    def srb_time(self) -> float:
        return self._proto_state.srb_time

    @srb_time.setter
    def srb_time(self, val: float):
        self._proto_state.srb_time = val
        self._y0[-1] = val

    @property
    def parachute_deployed(self) -> bool:
        return self._proto_state.parachute_deployed

    @parachute_deployed.setter
    def parachute_deployed(self, val: bool):
        self._proto_state.parachute_deployed = val

    @property
    def X(self):
        return self._y_components()[0]

    @property
    def Y(self):
        return self._y_components()[1]

    @property
    def VX(self):
        return self._y_components()[2]

    @property
    def VY(self):
        return self._y_components()[3]

    @property
    def Heading(self):
        return self._y_components()[4]

    @property
    def Spin(self):
        return self._y_components()[5]

    @property
    def Fuel(self):
        return self._y_components()[6]

    @property
    def Throttle(self):
        return self._y_components()[7]

    @property
    def LandedOn(self) -> Dict[int, int]:
        """Returns a mapping from index to index of entity landings.

        If the 0th entity is landed on the 2nd entity, 0 -> 2 will be mapped.
        """
        landed_map = {}
        for landed, landee in enumerate(
                self._y_components()[8]):
            if int(landee) != self.NO_INDEX:
                landed_map[landed] = int(landee)
        return landed_map

    @property
    def Broken(self):
        return self._y_components()[9]

    @property
    def Atmospheres(self) -> List[int]:
        """Returns a list of indexes of entities that have an atmosphere."""
        return self._entities_with_atmospheres

    @property
    def time_acc(self) -> float:
        """Returns the time acceleration, e.g. 1x or 50x."""
        return self._proto_state.time_acc

    @time_acc.setter
    def time_acc(self, new_acc: float):
        self._proto_state.time_acc = new_acc

    def craft_entity(self):
        """Convenience function, a full Entity representing the craft."""
        return self[self.craft]

    @property
    def craft(self) -> Optional[str]:
        """Returns the currently-controlled craft.
        Not actually backed by any stored field, just a calculation."""
        if common.HABITAT not in self._entity_names and \
                common.AYSE not in self._entity_names:
            return None
        if common.AYSE not in self._entity_names:
            return common.HABITAT

        hab_index = self._name_to_index(common.HABITAT)
        ayse_index = self._name_to_index(common.AYSE)
        if self._y_components()[8][hab_index] == ayse_index:
            # Habitat is docked with AYSE, AYSE is active craft
            return common.AYSE
        else:
            return common.HABITAT

    def reference_entity(self):
        """Convenience function, a full Entity representing the reference."""
        return self[self._proto_state.reference]

    @property
    def reference(self) -> str:
        """Returns current reference of the physics system, shown in GUI."""
        return self._proto_state.reference

    @reference.setter
    def reference(self, name: str):
        self._proto_state.reference = name

    def target_entity(self):
        """Convenience function, a full Entity representing the target."""
        return self[self._proto_state.target]

    @property
    def target(self) -> str:
        """Returns landing/docking target, shown in GUI."""
        return self._proto_state.target

    @target.setter
    def target(self, name: str):
        self._proto_state.target = name

    @property
    def navmode(self) -> Navmode:
        return Navmode(self._proto_state.navmode)

    @navmode.setter
    def navmode(self, navmode: Navmode):
        self._proto_state.navmode = navmode.value
