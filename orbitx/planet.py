import vpython

from orbitx import calc
from orbitx import state
from orbitx.displayable import Displayable
from orbitx.flight_gui import FlightGui


class Planet(Displayable):
    def __init__(self, entity: state.Entity, flight_gui: FlightGui) -> None:
        super(Planet, self).__init__(entity, flight_gui)
        self._obj = vpython.sphere(
            pos=entity.screen_pos(self.flight_gui.origin()),
            axis=calc.angle_to_vpy(entity.heading),
            up=vpython.vector(0, 0, 1),
            radius=entity.r * 0.9,
            make_trail=False,
            retain=10000,
            texture=self._texture,
            bumpmap=vpython.bumpmaps.gravel,
            shininess=Displayable.PLANET_SHININIESS
        )
        self._obj.name = self._entity.name
        self._draw_labels()
    # end of __init__

    def _draw_labels(self) -> None:
        self._label = self._create_label()
        self._label.text_function = lambda entity: entity.name
        self._label.text = self._label.text_function(self._entity)
    # end of _draw_labels

    def draw(self, entity: state.Entity):
        self._update_obj(entity)
    # end of draw
# end of class Planet
