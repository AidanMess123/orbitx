#!/usr/bin/env python3
import logging
import unittest

import numpy as np

import orbitx.common as common
import orbitx.physics as physics

log = logging.getLogger()

G = 6.67408e-11


class PhysicsEngine:
    """Ensures that the simthread is always shut down on test exit/failure."""

    def __init__(self, savefile):
        self.physics_engine = physics.PEngine(
            common.load_savefile(common.savefile(savefile)))
        self.physics_engine.set_time_acceleration(1, requested_t=0)

    def __enter__(self):
        return self.physics_engine

    def __exit__(self, *args):
        self.physics_engine._stop_simthread()


class PhysicsEngineTestCase(unittest.TestCase):
    @unittest.skip('Need Ye Qin to reimplement PhysicsEntity')
    def test_simple_collision(self):
        with PhysicsEngine('tests/simple-collision.json') as physics_engine:
            # In this case, the first entity is standing still and the second
            # on a collision course going left to right. The two should bounce.
            approach = physics_engine.get_state(40)
            bounced = physics_engine.get_state(60)
            self.assertTrue(approach.entities[0].x > approach.entities[1].x)
            self.assertTrue(approach.entities[1].vx > 0)
            self.assertTrue(bounced.entities[0].x > bounced.entities[1].x)
            self.assertTrue(bounced.entities[1].vx < 0)

    def test_basic_movement(self):
        with PhysicsEngine('tests/only-sun.json') as physics_engine:
            # In this case, the only entity is the Sun. It starts at (0, 0)
            # with a speed of (1, -1). It should move.
            initial = physics_engine.get_state(0)
            moved = physics_engine.get_state(10)
            self.assertEqual(initial.timestamp, 0)
            self.assertAlmostEqual(initial.entities[0].x, 0)
            self.assertAlmostEqual(initial.entities[0].y, 0)
            self.assertAlmostEqual(initial.entities[0].vx, 1)
            self.assertAlmostEqual(initial.entities[0].vy, -1)
            self.assertEqual(moved.timestamp, 10)
            self.assertAlmostEqual(moved.entities[0].x, 10)
            self.assertAlmostEqual(moved.entities[0].y, -10)
            self.assertAlmostEqual(moved.entities[0].vx, 1)
            self.assertAlmostEqual(moved.entities[0].vy, -1)

    def test_gravitation(self):
        with PhysicsEngine('tests/massive-objects.json') as physics_engine:
            # In this case, the first entity is very massive and the second
            # entity should gravitate towards the first entity.
            initial = physics_engine.get_state(0)
            moved = physics_engine.get_state(1)
            # https://www.wolframalpha.com/input/?i=1e30+kg+*+G+%2F+(1e8+m)%5E2
            # According to the above, this should be somewhere between 6500 and
            # 7000 m/s after one second.
            self.assertTrue(moved.entities[1].vx > 5000,
                            msg=f'vx is actually {moved.entities[1].vx}')

            # Test the internal math that the internal derive function is doing
            # the right calculations. Break out your SPH4U physics equations!!
            y0 = physics.Y(initial)
            # Note that dy.X is actually the velocity at 0,
            # and dy.VX is acceleration.
            dy = physics.Y(physics_engine._derive(0, y0._y_1d))
            self.assertEqual(dy._n, 2)
            self.assertAlmostEqual(dy.X[0], y0.VX[0])
            self.assertAlmostEqual(dy.Y[0], y0.VY[0])
            self.assertEqual(round(abs(dy.VX[0])),
                             round(G * initial.entities[1].mass /
                                   (y0.X[0] - y0.X[1])**2))
            self.assertAlmostEqual(dy.VY[0], 0)

            self.assertAlmostEqual(dy.X[1], y0.VX[1])
            self.assertAlmostEqual(dy.Y[1], y0.VY[1])
            self.assertEqual(round(abs(dy.VX[1])),
                             round(G * initial.entities[0].mass /
                                   (y0.X[1] - y0.X[0])**2))
            self.assertAlmostEqual(dy.VY[1], 0)

    def test_three_body(self):
        with PhysicsEngine('tests/three-body.json') as physics_engine:
            # In this case, three entities form a 90-45-45 triangle, with the
            # entity at the right angle being about as massive as the sun.
            # The first entity is the massive entity, the second is far to the
            # left, and the third is far to the top.
            state = physics_engine.get_state(0)

            # Test that every single entity has the correct accelerations.
            y0 = physics.Y(state)
            dy = physics.Y(physics_engine._derive(0, y0._y_1d))
            self.assertEqual(dy._n, 3)

            self.assertAlmostEqual(dy.X[0], y0.VX[0])
            self.assertAlmostEqual(dy.Y[0], y0.VY[0])
            self.assertEqual(round(abs(dy.VX[0])),
                             round(G * state.entities[1].mass /
                                   (y0.X[0] - y0.X[1])**2))
            self.assertEqual(round(abs(dy.VY[0])),
                             round(G * state.entities[2].mass /
                                   (y0.Y[0] - y0.Y[2])**2))

            self.assertAlmostEqual(dy.X[1], y0.VX[1])
            self.assertAlmostEqual(dy.Y[1], y0.VY[1])
            self.assertEqual(round(abs(dy.VX[1])),
                             round(G * state.entities[0].mass /
                                   (y0.X[1] - y0.X[0])**2 +

                                   np.sqrt(2) * G * state.entities[2].mass /
                                   (y0.X[1] - y0.X[2])**2
                                   ))
            self.assertEqual(round(abs(dy.VY[1])),
                             round(np.sqrt(2) * G * state.entities[2].mass /
                                   (y0.X[1] - y0.X[2])**2))

            self.assertAlmostEqual(dy.X[2], y0.VX[2])
            self.assertAlmostEqual(dy.Y[2], y0.VY[2])
            self.assertEqual(round(abs(dy.VX[2])),
                             round(np.sqrt(2) * G * state.entities[2].mass /
                                   (y0.X[1] - y0.X[2])**2))
            self.assertEqual(round(abs(dy.VY[2])),
                             round(
                             G * state.entities[0].mass /
                             (y0.Y[2] - y0.Y[0])**2 +

                             np.sqrt(2) * G * state.entities[1].mass /
                             (y0.Y[2] - y0.Y[1])**2
                             ))


if __name__ == '__main__':
    unittest.main()
