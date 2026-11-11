"""Synthetic regression tests; confidential integration runs are opt-in.

Run: python3 -m unittest discover -s tests -v
Optional: SCREENPLAY_DIR=/private/path python3 -m unittest discover -s tests -v
"""

import math
import os
from pathlib import Path
import unittest

from promo_finder.parser import (
    format_timestamp,
    parse_episode,
    parse_text,
    to_centiseconds,
)
from promo_finder.validator import (
    ValidationError,
    rank_and_deduplicate,
    validate_candidate,
)


SYNTHETIC = '''episode number: Episode 3
episode logline: Synthetic input for tests.
continuity notes: These notes must not replace evidence inside a cut.

--- SCRIPT START ---

scene 01
Scene heading: INT. SIGNAL ROOM - NIGHT
Scene start: 00:00.00
Scene end: 01:10.00

Actions and dialogue:

**camera:**
Start: (00:00.00) | End: (00:05.00)
A warning lamp flashes above the platform gate.

**action:**
Start: (00:00.00) | End: (00:02.00)
A porter slams the emergency brake.

**DISPATCHER (Alarmed):**
Start: (00:00.50) | End: (00:04.00)
"The last train has no driver."

**CAMERA:**
Start: (00:05.00) | End: (00:25.50)
The porter runs to the control desk.

**DISPATCHER (Urgent):**
Start: (00:24.00) | End: (00:27.00)
"Only this switch can stop it."

**CAMERA:**
Start: (00:25.50) | End: (00:32.00)
The dispatcher reaches for the locked switch.

**ACTION:**
Start: (00:29.00) | End: (00:31.00)
The switch refuses to move; a timer reads ten seconds.

**DISPATCHER:**
Start: (00:31.00) | End: (00:34.00)
"Who has the key?"

**CAMERA:**
Start: (00:32.00) | End: (00:40.00)
The porter turns toward a locked cabinet.

**SOUND FX:**
Start: (00:60.00) | End: (01:01.00)
A distant bell.

--- SCRIPT END ---
'''.replace('00:60.00', '01:00.00')


def candidate(**overrides):
    result = {
        'start_event_id': 'E003-0001',
        'end_event_id': 'E003-0007',
        'hook_event_id': 'E003-0002',
        'cliffhanger_event_id': 'E003-0007',
        'hook': 'A porter pulls the emergency brake.',
        'cliffhanger': 'The switch is locked while a train approaches.',
        'why_it_works': 'An immediate threat leads to an unfinished rescue.',
        'hook_score': 5,
        'cliffhanger_score': 4,
        'clarity_score': 3,
    }
    result.update(overrides)
    return result


class TimestampTests(unittest.TestCase):
    def test_manual_benchmark_uses_exact_integer_arithmetic(self):
        start = to_centiseconds('01:24.00')
        end = to_centiseconds('02:20.50')
        self.assertEqual(start, 8400)
        self.assertEqual(end, 14050)
        self.assertEqual(end - start, 5650)
        self.assertEqual(f'{(end - start) / 100:.2f}', '56.50')

    def test_round_trip_keeps_hundredths(self):
        for stamp in ('00:00.00', '00:00.01', '00:59.99', '01:24.00', '02:20.50'):
            with self.subTest(stamp=stamp):
                self.assertEqual(format_timestamp(to_centiseconds(stamp)), stamp)

    def test_malformed_timestamps_are_rejected(self):
        for stamp in ('00:60.00', '-1:00.00', 'garbage', '00:01.999'):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                to_centiseconds(stamp)


class ParserTests(unittest.TestCase):
    def test_episode_number_comes_from_case_insensitive_metadata(self):
        episode = parse_text(SYNTHETIC, source_name='episode-999-misleading.txt')
        self.assertEqual(episode.number, 3)
        self.assertEqual(len(episode.scenes), 1)
        self.assertEqual(len(episode.events), 10)
        self.assertEqual(episode.events[0].id, 'E003-0001')
        self.assertEqual(episode.events[-1].id, 'E003-0010')

    def test_overlapping_blocks_preserve_original_timecodes_and_labels(self):
        events = parse_text(SYNTHETIC).events
        self.assertEqual(events[0].start, '00:00.00')
        self.assertEqual(events[1].start, '00:00.00')
        self.assertEqual(events[2].start_cs, 50)
        self.assertEqual(events[0].end_cs, 500)
        self.assertNotEqual(events[0].id, events[1].id)
        self.assertEqual(events[0].type.casefold(), 'camera')
        self.assertEqual(events[1].type.casefold(), 'action')
        self.assertEqual(events[2].type.casefold(), 'dialogue')
        self.assertIn('DISPATCHER', events[2].label)
        self.assertIn('"The last train has no driver."', events[2].original_text)

    def test_multiline_body_survives(self):
        text = SYNTHETIC.replace(
            'The porter runs to the control desk.',
            'The porter runs to the control desk.\nA second warning lamp flashes.',
        )
        event = parse_text(text).events[3]
        self.assertIn('desk.\nA second warning', event.original_text)

    def test_missing_timed_block_cannot_silently_reduce_entry_count(self):
        text = SYNTHETIC.replace('**action:**', 'UNRECOGNIZED BLOCK HEADER')
        with self.assertRaises(ValueError):
            parse_text(text)

    def test_negative_event_duration_is_rejected(self):
        text = SYNTHETIC.replace(
            'Start: (00:29.00) | End: (00:31.00)',
            'Start: (00:31.00) | End: (00:29.00)',
        )
        with self.assertRaises(ValueError):
            parse_text(text)

    def test_event_crossing_declared_scene_end_is_preserved_with_warning(self):
        text = SYNTHETIC.replace('Scene end: 01:10.00', 'Scene end: 00:30.00')
        text = text.replace(
            '**SOUND FX:**',
            'SCENE 02\nScene heading: EXT. PLATFORM - NIGHT\n'
            'Scene start: 00:30.00\nScene end: 01:10.00\n\n**SOUND FX:**',
        )
        episode = parse_text(text)
        self.assertEqual(len(episode.events), 10)
        self.assertEqual(len(episode.scenes), 2)
        self.assertEqual(episode.events[6].end_cs, 3100)
        self.assertTrue(episode.metadata['parser_warnings'])


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.episode = parse_text(SYNTHETIC)

    def test_boundaries_come_from_start_and_end_events(self):
        result = validate_candidate(self.episode, candidate())
        self.assertEqual(result['start_cs'], 0)
        self.assertEqual(result['end_cs'], 3100)
        self.assertEqual(float(result['duration']), 31.0)
        self.assertEqual(result['start'], '00:00.00')
        self.assertEqual(result['end'], '00:31.00')
        self.assertFalse(result['review_flags'])

    def test_each_of_the_four_event_ids_is_checked(self):
        for field in ('start_event_id', 'end_event_id', 'hook_event_id', 'cliffhanger_event_id'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_candidate(self.episode, candidate(**{field: 'E004-0001'}))

    def test_late_hook_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_candidate(self.episode, candidate(hook_event_id='E003-0004'))

    def test_hook_that_began_before_the_cut_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_candidate(self.episode, candidate(start_event_id='E003-0004'))

    def test_hook_exactly_three_seconds_after_start_is_valid(self):
        text = SYNTHETIC.replace(
            'Start: (00:00.00) | End: (00:02.00)',
            'Start: (00:03.00) | End: (00:04.00)',
        )
        result = validate_candidate(parse_text(text), candidate())
        self.assertEqual(result['duration'], 31.0)

    def test_cliffhanger_must_land_before_end(self):
        with self.assertRaises(ValidationError):
            validate_candidate(self.episode, candidate(cliffhanger_event_id='E003-0008'))

    def test_cliffhanger_that_began_before_cut_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_candidate(self.episode, candidate(
                start_event_id='E003-0004', end_event_id='E003-0009',
                hook_event_id='E003-0004', cliffhanger_event_id='E003-0003',
            ))

    def test_short_reversed_and_overlong_cuts_are_rejected(self):
        changes = (
            {'end_event_id': 'E003-0003'},
            {'start_event_id': 'E003-0009', 'end_event_id': 'E003-0007'},
            {'end_event_id': 'E003-0010'},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_candidate(self.episode, candidate(**change))

    def test_scores_are_finite_numbers_in_zero_to_five(self):
        for field in ('hook_score', 'cliffhanger_score', 'clarity_score'):
            for value in (True, '5', -1, 6, math.nan, math.inf):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    validate_candidate(self.episode, candidate(**{field: value}))

    def test_camera_end_can_still_bisect_spoken_dialogue(self):
        result = validate_candidate(self.episode, candidate(end_event_id='E003-0006'))
        self.assertEqual(result['end_cs'], 3200)
        self.assertTrue(result['review_flags'])
        self.assertIn('dialogue', ' '.join(result['review_flags']).casefold())

    def test_camera_start_can_bisect_an_overlapping_line(self):
        result = validate_candidate(self.episode, candidate(
            start_event_id='E003-0006', end_event_id='E003-0010',
            hook_event_id='E003-0006', cliffhanger_event_id='E003-0010',
        ))
        self.assertTrue(result['review_flags'])
        self.assertIn('start_inside_dialogue', ' '.join(result['review_flags']).casefold())

    def test_duration_limits_include_exactly_twenty_and_sixty_seconds(self):
        for start, end in (('00:18.00', '00:20.00'), ('00:59.00', '01:00.00')):
            text = SYNTHETIC.replace(
                'Start: (00:29.00) | End: (00:31.00)',
                f'Start: ({start}) | End: ({end})',
            )
            with self.subTest(end=end):
                result = validate_candidate(parse_text(text), candidate())
                self.assertIn(result['duration'], (20.0, 60.0))

    def test_heavy_overlap_with_same_evidence_keeps_higher_score(self):
        high = validate_candidate(self.episode, candidate())
        low = validate_candidate(self.episode, candidate(end_event_id='E003-0006', hook_score=3))
        high['candidate_id'] = 'higher'
        low['candidate_id'] = 'lower'
        ranked, rejected = rank_and_deduplicate([low, high])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]['candidate_id'], 'higher')
        self.assertEqual(ranked[0]['rank'], 1)
        self.assertEqual(len(rejected), 1)

    def test_overlap_with_different_cliffhanger_evidence_is_retained(self):
        first = validate_candidate(self.episode, candidate())
        second = validate_candidate(self.episode, candidate(
            end_event_id='E003-0009', cliffhanger_event_id='E003-0006',
        ))
        ranked, rejected = rank_and_deduplicate([first, second])
        self.assertEqual(len(ranked), 2)
        self.assertFalse(rejected)

    def test_score_uses_forty_thirty_five_twenty_five_weights(self):
        result = validate_candidate(self.episode, candidate())
        self.assertAlmostEqual(result['score'], 4.15)


@unittest.skipUnless(os.environ.get('SCREENPLAY_DIR'), 'Set SCREENPLAY_DIR for private input checks')
class PrivatePackTests(unittest.TestCase):
    def test_complete_pack_and_episode_three_benchmark(self):
        folder = Path(os.environ['SCREENPLAY_DIR'])
        episodes = [parse_episode(path) for path in sorted(folder.glob('episode-*.txt'))]
        self.assertEqual(len(episodes), 50)
        self.assertEqual({ep.number for ep in episodes}, set(range(1, 51)))
        self.assertEqual(sum(len(ep.scenes) for ep in episodes), 100)
        self.assertEqual(sum(len(ep.events) for ep in episodes), 3727)
        episode = next(ep for ep in episodes if ep.number == 3)
        starts = [e for e in episode.events if e.start == '01:24.00']
        ends = [e for e in episode.events if e.end == '02:20.50']
        self.assertTrue(starts)
        self.assertTrue(ends)
        self.assertEqual(ends[0].end_cs - starts[0].start_cs, 5650)


if __name__ == '__main__':
    unittest.main()
