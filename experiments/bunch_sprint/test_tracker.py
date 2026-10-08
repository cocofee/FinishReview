"""Counting invariants for the experimental rider tracker."""

import unittest

import numpy as np

from .tracker import RiderTracker, appearance, centre, fuse


class RiderTrackerTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.full((500, 1000, 3), 120, dtype=np.uint8)
        self.tracker = RiderTracker('test', 1000, 25)

    def person(self, x, y=170):
        box = [x - 30, y - 70, x + 30, y + 70, .9]
        return {'box': box, 'kind': 'person', 'point': centre(box),
                'appearance': appearance(self.frame, box)}

    def test_two_riders_crossing_in_one_frame_remain_distinct(self):
        for index, x in enumerate((450, 490, 540, 580)):
            self.tracker.update(index, [self.person(x), self.person(x, 360)], self.frame)
        self.assertEqual(len(self.tracker.events), 2)
        self.assertEqual(len({event['track_id'] for event in self.tracker.events}), 2)
        self.assertEqual({event['frame_after'] for event in self.tracker.events}, {2})

    def test_reverse_flow_does_not_emit_forward_crossings(self):
        for index, x in enumerate((600, 560, 520, 480, 440)):
            self.tracker.update(index, [self.person(x)], self.frame)
        self.assertEqual(self.tracker.events, [])

    def test_one_track_is_counted_once_despite_line_jitter(self):
        for index, x in enumerate((450, 490, 540, 500, 550)):
            self.tracker.update(index, [self.person(x)], self.frame)
        self.assertEqual(len(self.tracker.events), 1)

    def test_crossing_interpolated_across_detection_gap_is_tentative(self):
        for index, x in ((0, 450), (1, 490), (3, 550)):
            self.tracker.update(index, [self.person(x)], self.frame)
        self.assertEqual(len(self.tracker.events), 1)
        self.assertEqual(self.tracker.events[0]['status'], 'tentative')
        self.assertEqual(self.tracker.events[0]['gap'], 2)

    def test_shared_bicycle_supports_two_people_without_adding_a_rider(self):
        people = [[350, 100, 430, 260, .9], [520, 100, 600, 260, .8]]
        bicycle = [[330, 150, 630, 350, .8]]
        observations, audit = fuse(people, bicycle, self.frame)
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(obs['kind'] == 'person' for obs in observations))
        self.assertTrue(all(obs['merged_bike'] for obs in observations))
        self.assertEqual(len(audit['merged']), 1)

    def test_nested_person_duplicate_is_not_a_second_rider(self):
        people = [[350, 100, 430, 260, .9], [349, 99, 431, 261, .5]]
        observations, audit = fuse(people, [], self.frame)
        self.assertEqual(len(observations), 1)
        self.assertEqual(len(audit['suppressed']), 1)


if __name__ == '__main__':
    unittest.main()
