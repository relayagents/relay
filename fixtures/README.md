# Fixtures

Everything here is synthetic. No real meetings, people, audio, or credentials.

- `transcript_sample.json`: a made-up 60-second research sync between `ada`, `grace`, and `linus`.
  It uses explicit cue phrases (`Decision:`, `Action:`, `<name> will ...`, `Question for <name>:`) so the
  deterministic `KeywordExtractor` finds two decisions, two action items, and one question. The LLM
  extractor should find the same plus the softer "I'll check the grant line" follow-up.
- `sample_audio.wav`: two seconds of sine tones, generated in code. It exists so the upload path can be
  exercised end to end; WhisperX will produce an empty or nonsense transcript for it, which is expected.
