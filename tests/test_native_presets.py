"""Keep preset requests on the native text-to-speech prompt path."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tts_engine.pacing import phrase_chunks


class NativePresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_paragraph_mode_avoids_the_reported_generation_reset(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator

        class Core(SvaraTTSOrchestrator):
            def __init__(self):
                self.max_chunk_chars = 200
                self.calls = []

            async def _astream_one(self, text, **kwargs):
                self.calls.append((text, kwargs))
                yield bytes(4800)

        text = ('निसर्ग हा माणसाचा खरा मित्र आहे. झाडे, नद्या, डोंगर आणि पशू-पक्ष्यांनी मिळून हा सुंदर निसर्ग तयार झाला आहे. '
                'निसर्गामुळे आपल्याला शुद्ध हवा, पाणी आणि जगण्यासाठी अन्न मिळते. पण आजच्या काळात माणूस आपल्या स्वार्थासाठी '
                'जंगले तोडत आहे आणि प्रदूषण वाढवत आहे. जर आपण आता निसर्गाचे रक्षण केले नाही, तर भविष्यात मोठे संकट येऊ शकते. '
                'म्हणून आपण जास्तीत जास्त झाडे लावली पाहिजेत आणि पर्यावरणाचा समतोल राखला पाहिजे.')
        core = Core()
        core.max_paragraph_chars = 1000
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '4096'}):
            [part async for part in core.astream(text, speaker_id='Marathi (Female)', steady_volume=False)]
        self.assertEqual(len(core.calls), 1)
        self.assertEqual(core.calls[0][0], text)
        self.assertIsNone(core.calls[0][1]['audio_reference'])
        from tts_engine.voice_config import get_all_voices, get_speaker_id
        for voice in get_all_voices(model_id='svara-tts-v1'):
            core.calls.clear()
            with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '4096'}):
                [part async for part in core.astream(text, chunk_size=1000, max_tokens=4096,
                                                   speaker_id=get_speaker_id(voice.voice_id),
                                                   steady_volume=False)]
            self.assertEqual(len(core.calls), 1, voice.voice_id)
            self.assertEqual(core.calls[0][0], text)
            self.assertEqual(core.calls[0][1]['speaker_id'], get_speaker_id(voice.voice_id))
            self.assertIsNone(core.calls[0][1]['audio_reference'])
        core.calls.clear()
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '4096'}):
            [part async for part in core.astream(text, speaker_id='Hindi (Male)', steady_volume=False)]
        self.assertGreater(len(core.calls), 1)
        self.assertEqual(' '.join(part for part, _ in core.calls), text)
        core.calls.clear()
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '2048'}):
            [part async for part in core.astream(text, chunk_size=1000, steady_volume=False)]
        self.assertGreater(len(core.calls), 1)
        self.assertEqual(' '.join(part for part, _ in core.calls), text)

    async def test_native_budget_can_finish_the_approved_paragraph(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator

        class Transport:
            async def astream(self, prompt, **kwargs):
                self.options = kwargs
                yield ''.join(f'<custom_token_{10 + band * 4096}>' for band in range(7))

        core = SvaraTTSOrchestrator.__new__(SvaraTTSOrchestrator)
        core.speaker_id = 'Marathi (Female)'
        core.tokenizer = SimpleNamespace(encode=lambda *args, **kwargs: [1] * 300)
        core.transport = Transport()
        core.codec = SimpleNamespace(decode_codes=lambda codes: b'\0\0' * 2048)
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '4096'}), \
                patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt'):
            [part async for part in core._astream_one('A full paragraph')]
            self.assertGreater(core.transport.options['max_tokens'], 2436)
            self.assertLessEqual(core.transport.options['max_tokens'] + 300 + 8, 4096)
            [part async for part in core._astream_one('A full paragraph', max_tokens=500)]
            self.assertEqual(core.transport.options['max_tokens'], 500)
            [part async for part in core._astream_one('A full paragraph', audio_reference=[128266] * 7)]
            self.assertEqual(core.transport.options['max_tokens'], 2048)

    async def test_long_presets_never_gain_automatic_audio_references(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator

        class Transport:
            async def astream(self, prompt, **kwargs):
                yield ''.join(f'<custom_token_{10 + band * 4096}>' for band in range(7))

        core = SvaraTTSOrchestrator.__new__(SvaraTTSOrchestrator)
        core.speaker_id = 'English (Female)'
        core.max_chunk_chars = 100
        core.tokenizer = SimpleNamespace(encode=lambda *args, **kwargs: [1] * 40)
        core.transport = Transport()
        core.codec = SimpleNamespace(decode_codes=lambda codes: b'\0\0' * 2048)
        text = ('निसर्ग हा माणसाचा खरा मित्र आहे. झाडे, नद्या, डोंगर आणि पशू-पक्ष्यांनी '
                'मिळून हा सुंदर निसर्ग तयार झाला आहे. जर आपण आता निसर्गाचे रक्षण केले नाही, '
                'तर भविष्यात मोठे संकट येऊ शकते. म्हणून आपण जास्तीत जास्त झाडे लावली पाहिजेत.')
        expected = phrase_chunks(text, 100)
        self.assertGreater(len(expected), 1)
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt') as build:
            output = [part async for part in core.astream(text, speaker_id='Marathi (Female)')]
        self.assertTrue(output)
        self.assertEqual([call.kwargs['text'] for call in build.call_args_list], expected)
        for call in build.call_args_list:
            self.assertEqual(call.kwargs['speaker_id'], 'Marathi (Female)')
            self.assertIsNone(call.kwargs['audio_tokens'])
            self.assertIsNone(call.kwargs['transcript'])
        # User-requested voice references still pass through without changing
        # their transcript or inserting preset labels into their text.
        with patch('tts_engine.orchestrator.svara_text_to_tokens', return_value='prompt') as build:
            [part async for part in core.astream(
                text, audio_reference=[128266] * 7, reference_text='User transcript')]
        for call in build.call_args_list:
            self.assertEqual(call.kwargs['audio_tokens'], [128266] * 7)
            self.assertEqual(call.kwargs['transcript'], 'User transcript')
            self.assertIn(call.kwargs['text'], expected)


class VoiceContinuityTests(unittest.IsolatedAsyncioTestCase):
    """A cut inside a sentence starts a new generation, and the model draws a
    new voice for every generation. Keep sentences whole and cut only at pauses."""

    LONG = ('आज सकाळी मी घरातून निघून बाजारात गेलो होतो, तिथे मला माझे अनेक जुने मित्र भेटले ज्यांना मी खूप '
            'वर्षांपासून भेटलो नव्हतो, आम्ही खूप वेळ बसून जुन्या गप्पा मारल्या आणि सर्वांनी मिळून गरम चहा '
            'घेतला, त्यानंतर मी आनंदाने घरी परत आलो आणि दिवसभर माझे काम करत राहिलो.')

    def test_a_long_sentence_stays_in_one_phrase_within_the_whole_sentence_limit(self):
        from tts_engine.pacing import speech_units
        self.assertGreater(speech_units(self.LONG), 60)
        self.assertEqual(phrase_chunks(self.LONG, sentence_chars=700, sentence_units=125), [self.LONG])
        # Without the relaxed limit the old behaviour of cutting is retained.
        self.assertGreater(len(phrase_chunks(self.LONG)), 1)

    def test_forced_cuts_fall_after_pauses_and_leave_no_tiny_leftover(self):
        from tts_engine.pacing import speech_units
        text = ' '.join([self.LONG.rstrip('.')] * 3) + '.'
        parts = phrase_chunks(text, sentence_chars=700, sentence_units=125)
        self.assertGreater(len(parts), 1)
        self.assertEqual(' '.join(parts), text)
        for part in parts:
            self.assertLessEqual(speech_units(part), 125)
            self.assertLessEqual(len(part), 700)
        for part in parts[:-1]:
            self.assertTrue(part.endswith(','), part[-20:])
        sizes = [speech_units(part) for part in parts]
        self.assertGreaterEqual(min(sizes), .5 * max(sizes))

    def test_a_sentence_without_pauses_is_still_cut_evenly(self):
        from tts_engine.pacing import speech_units
        text = ' '.join(['Please speak clearly and naturally'] * 30) + '.'
        parts = phrase_chunks(text)
        self.assertEqual(' '.join(parts), text)
        sizes = [speech_units(part) for part in parts]
        self.assertLessEqual(max(sizes), 60)
        self.assertGreaterEqual(min(sizes), .6 * max(sizes))

    async def test_presets_generate_a_whole_long_sentence_once_unless_a_limit_is_requested(self):
        from tts_engine.orchestrator import SvaraTTSOrchestrator
        from tts_engine.voice_config import get_all_voices, get_speaker_id

        class Core(SvaraTTSOrchestrator):
            def __init__(self):
                self.max_chunk_chars = 200
                self.calls = []

            async def _astream_one(self, text, **kwargs):
                self.calls.append(text)
                yield bytes(4800)

        core = Core()
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '4096'}):
            for voice in get_all_voices(model_id='svara-tts-v1'):
                core.calls.clear()
                [part async for part in core.astream(
                    self.LONG, speaker_id=get_speaker_id(voice.voice_id), steady_volume=False)]
                self.assertEqual(core.calls, [self.LONG], voice.voice_id)
            core.calls.clear()
            [part async for part in core.astream(self.LONG, chunk_size=100, steady_volume=False)]
            self.assertGreater(len(core.calls), 1)
            core.calls.clear()
            # A reference voice keeps its previous limits.
            [part async for part in core.astream(
                self.LONG, audio_reference=[128266] * 7, steady_volume=False)]
            self.assertGreater(len(core.calls), 1)
        with patch.dict('os.environ', {'VLLM_MAX_MODEL_LEN': '2048'}):
            core.calls.clear()
            [part async for part in core.astream(self.LONG * 3, steady_volume=False)]
            self.assertGreater(len(core.calls), 1)


if __name__ == '__main__':
    unittest.main()
