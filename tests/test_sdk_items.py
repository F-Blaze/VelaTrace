"""Construct the real pinned SDK objects offline; this is not a KiCad IPC test."""
from types import SimpleNamespace
import unittest

from kipy.board_types import BoardSegment, BoardText, Net, Track, Via
from kipy.proto.board.board_types_pb2 import Net as NetProto
from kipy.proto.board.board_types_pb2 import VT_THROUGH
from kipy.proto.board.board_types_pb2 import BL_F_Cu, BL_B_Cu, BL_User_9
from kipy.proto.common.types.enums_pb2 import SLS_DASH

from velatrace.candidate import CopperItem
from velatrace.errors import ValidationError
from velatrace.ses import RoutePlan, Track as PlannedTrack, Via as PlannedVia, ViaSpec
from velatrace.write_safety import ItemFactory


class SdkItemTests(unittest.TestCase):
    def setUp(self):
        self.net = Net(NetProto(name='SIGNAL'))
        self.board = SimpleNamespace(get_nets=lambda: [self.net])

    def test_copper_is_real_sdk_track_and_through_via_in_nanometers(self):
        items = (CopperItem('11111111-1111-4111-8111-111111111111', 'segment', 'SIGNAL', 'B.Cu',
                            (1.125, 2.25), (3.5, 4.625), .25),
                 CopperItem('22222222-2222-4222-8222-222222222222', 'via', 'SIGNAL', 'F.Cu',
                            (3.5, 4.625), None, .6, .3))
        track, via = ItemFactory.copper(items, self.board)
        self.assertIsInstance(track, Track)
        self.assertIsInstance(via, Via)
        self.assertEqual((track.start.x, track.start.y), (1125000, 2250000))
        self.assertEqual((track.end.x, track.end.y), (3500000, 4625000))
        self.assertEqual(track.width, 250000)
        self.assertEqual(track.layer, BL_B_Cu)
        self.assertEqual(track.net.name, 'SIGNAL')
        self.assertEqual(track.id.value, items[0].id)
        self.assertEqual(via.diameter, 600000)
        self.assertEqual(via.drill_diameter, 300000)
        self.assertEqual(via.proto.type, VT_THROUGH)
        self.assertEqual(via.proto.pad_stack.drill.start_layer, BL_F_Cu)
        self.assertEqual(via.proto.pad_stack.drill.end_layer, BL_B_Cu)
        self.assertFalse(track.locked)
        self.assertFalse(via.locked)

    def test_preview_dashed_graphics_flip_y_once_and_never_create_copper(self):
        plan = RoutePlan('fixture', (PlannedTrack('SIGNAL', 'B.Cu', .25, ((1, -2), (3, -4))),),
                         (PlannedVia('SIGNAL', (3, -4), ViaSpec(.6, .3, ('F.Cu', 'B.Cu'))),))
        items = ItemFactory.preview(plan, BL_User_9)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(isinstance(item, BoardSegment) for item in items))
        self.assertEqual((items[0].start.x, items[0].start.y), (1000000, 2000000))
        self.assertEqual((items[0].end.x, items[0].end.y), (3000000, 4000000))
        self.assertTrue(all(item.layer == BL_User_9 and item.attributes.stroke.style == SLS_DASH
                            for item in items))
        self.assertEqual(len({item.id.value for item in items}), 3)

    def test_annotation_plain_text_and_stale_mapping_refusal(self):
        annotation = ItemFactory.annotations([('critical\nU1\r<text>', 1, 2)], BL_User_9)[0]
        self.assertIsInstance(annotation, BoardText)
        self.assertEqual(annotation.value, 'critical U1 <text>')
        self.assertEqual(annotation.layer, BL_User_9)
        with self.assertRaises(ValidationError):
            ItemFactory.annotations([('x' * 501, 1, 2)], BL_User_9)
        with self.assertRaises(ValidationError):
            ItemFactory.copper([CopperItem('id', 'segment', 'MISSING', 'F.Cu', (0, 0), (1, 1), .25)], self.board)


if __name__ == '__main__':
    unittest.main()
