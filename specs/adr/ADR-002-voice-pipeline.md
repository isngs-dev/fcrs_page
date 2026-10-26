# ADR-002 — Real-time voice pipeline: Plivo (turn-based first, audio streaming second)

- **Status:** Proposed · rev 2 · 2026-10-15 · **provider fixed by product owner: Plivo** (rev 1 recommended
  Twilio ConversationRelay; superseded).
- **Context:** Calls need fast replies, barge-in and silence handling; US only, English, recording on.
  Today:
  - Plivo already runs browser "Call Us" (`voice_agent/`): `<GetInput inputType="speech">` →
    Plivo's ASR posts text to our turn webhook → we answer with `<Speak>` (Plivo TTS) → `<Dial>` for
    transfer. Proven in production, including the `CallSession` silence/repeat/transfer rules.
  - `voice/` OpenAI ASR/TTS is batch-only.
  - Droplet: 1.9 GB RAM, no swap.
  - `gpt-oss:20b`: 3.7–4.5 s per grounded reply (measured 2026-10-14).
  - Plivo has **no ConversationRelay equivalent** (no managed text-over-WebSocket STT/TTS relay).

## Options on Plivo

| | **P1. Turn-based XML** (`<GetInput speech>` + `<Speak>`) | **P2. Audio Streaming** (`<Stream>` WebSocket + streaming STT/TTS) |
|---|---|---|
| How | Plivo does ASR + TTS; we exchange text per turn over HTTP webhooks | Raw μ-law 8 kHz audio both ways; we pipe to a streaming STT (e.g. Deepgram) and streaming TTS (e.g. ElevenLabs/Deepgram), run end-of-turn + barge-in logic (Plivo suggests Pipecat) |
| Reuse | **Same pattern as `voice_agent/`** (code + tests exist) | New audio stack |
| Latency per turn | Plivo end-of-speech + LLM + Plivo TTS ≈ **2–4 s** with a fast model | **≈ 1 s** achievable with a fast streaming model |
| Barge-in | **Unverified** for `<Speak>` nested in `<GetInput>` (not documented) | Full control |
| Our RAM/CPU | Minimal (HTTP) | Moderate: per-call WebSockets + audio relay (STT/TTS hosted, no local models); new deps |
| Vendors / cost | Plivo only | Plivo + STT + TTS vendors (more cost, more DPAs) |
| Effort | **Low–medium** | High |

## Decision

1. **A6 ships P1** — generalise the existing `voice_agent` Plivo turn loop into the Calling Agent
   (outbound + inbound), behind a `TelephonyProvider` Protocol. Lowest risk, reuses tested code, fits
   the droplet.
2. **A6b (conditional) upgrades to P2** behind the same Protocol if measured p50 turn latency > 2.5 s or
   barge-in is not achievable with P1. Decision taken with A6 measurements, not now.
3. Either way the **LLM is the dominant latency**: voice turns use a per-tenant fast, non-reasoning
   `voice_model` (decision Q8 still open), with the existing `reasoning_effort="low"` path for reasoning
   models as the fallback.

## Plivo capabilities used (checked 2026-10-15)

- **Answering-machine detection:** outbound only, asynchronous — `machine_detection` (`true`/`hangup`),
  `machine_detection_time` (default 5000 ms), result `Machine=true` posted to `machine_detection_url`;
  switch the call flow with the Transfer API.
- **Recording:** `<Record>` XML (with `<Dial>`/`startOnDialAnswer`) or the Record-call API on a live call;
  WAV/MP3; stereo for regular calls. Plivo does **not** announce or collect consent — we must (US
  all-party-consent states: CA, CT, FL, IL, MD, MA, MT, NV, NH, PA, WA — `compliance.md` §5).
- **Transfer:** `<Dial>` to the tenant's transfer number (already used by `voice_agent`).
- **Audio Streaming (P2):** `<Stream>` to a `wss://` endpoint; events start/media/dtmf/stop; `playAudio`
  for replies; μ-law 8 kHz recommended.

## Consequences

- Per-tenant Plivo credentials + numbers (encrypted, `telephony_numbers`); the existing platform Plivo
  account keeps serving browser Call Us until consolidation (Q6/Q7).
- Turn webhooks reuse the `voice_agent` signature verification (`X-Plivo-Signature-V3`) and HMAC call
  tickets.

## Sources (checked 2026-10-15)

- Plivo machine detection: https://www.plivo.com/docs/voice/concepts/machine-detection/ ·
  https://www.plivo.com/docs/voice/api/call/asynchronous-machine-detection/
- Plivo GetInput XML: https://www.plivo.com/docs/voice/xml/getinput/
- Plivo call recording: https://docs.plivo.com/docs/voice/use-cases/record-a-call · https://docs.plivo.com/docs/voice/xml/record.md
- Plivo Audio Streaming: https://www.plivo.com/docs/voice-agents/audio-streaming/overview
- Recording consent by state (third-party): https://telnyx.com/resources/call-recording-what-is-isnt-legal.md
