## video-codecs | Compare H.264, HEVC, VP9 and AV1. What decides which one you ship?
- H.264/AVC: universal hardware decode, patent-licensed; profile and level (High@4.1) gate device support
- HEVC: roughly 30-50% fewer bits than H.264 at equal quality; several patent pools, unclear royalties
- VP9 (Google) and AV1 (AOMedia) are royalty-free; AV1 saves roughly 30% over VP9/HEVC
- AV1 encodes slowly (libaom; SVT-AV1 is faster) and needs recent hardware decode; fall back to H.264
- Decision: device decode coverage, licensing, encode cost vs CDN savings at your view count

## video-gop | What are I/P/B frames and a GOP? Why do keyframes matter for streaming?
- I = self-contained, P = predicts from past frames, B = predicts from past and future frames
- GOP = frames from one keyframe to the next; an IDR frame resets the decoder's reference state
- Seeking and quality switching can only start on a keyframe; ABR segments must start on one
- Longer GOP compresses better but slows seeks and worsens error recovery
- B-frames force decode order != display order, so DTS differs from PTS

## video-rate-control | CRF vs CBR vs VBR vs 2-pass. What is VBV and why does streaming need it?
- CRF: constant quality, bitrate varies with content; best for file-size efficiency
- CBR/ABR: target an average or constant bitrate; 2-pass spends bits better for a known target
- VBV (-maxrate, -bufsize) models the decoder buffer and caps bitrate peaks
- Streaming uses capped CRF: quality-driven, but peaks bounded so segments fit the ladder rung and network

## video-container | Container vs codec. MP4, fragmented MP4, MPEG-TS and WebM: when is each used?
- Codec compresses the stream; container muxes audio, video, subtitles and timing metadata
- MP4 (ISO BMFF): moov (index) + mdat (data); +faststart puts moov first for progressive playback
- Fragmented MP4: moof+mdat fragments, needed for live and for HLS/DASH segments (CMAF)
- MPEG-TS: 188-byte packets, legacy HLS and broadcast; HEVC in HLS needs fMP4
- WebM/Matroska: VP9, AV1, Opus; open container

## video-abr | How does adaptive bitrate streaming work? Compare HLS and DASH.
- Encode one source into a ladder of renditions (resolution/bitrate), cut into 2-10 s segments
- Keyframes aligned across renditions so the player can switch at any segment boundary
- Manifest lists renditions and segments; player downloads over plain HTTP, so CDNs cache it
- HLS: Apple, .m3u8 playlists, native in Safari/iOS; DASH: ISO standard, .mpd XML manifest
- Shared fMP4/CMAF segments let one packaged set serve both

## video-abr-algo | How does a player choose the next rendition? Name the algorithm families.
- Throughput-based: smoothed (EWMA) segment download speed picks the highest rung that fits
- Buffer-based (BBA, BOLA): buffer level drives the choice; robust to noisy throughput estimates
- Hybrid/MPC: model predictive control or RL (Pensieve) trade quality, stalls and switches
- Goals: avoid rebuffering, high average quality, few switches; hysteresis prevents oscillation
- Start conservatively (low rung, small first segments) for fast startup

## video-cmaf | What is CMAF and why does it matter?
- Common Media Application Format: standard fMP4 segments with chunked structure
- One set of segments serves both HLS and DASH: half the storage and better CDN cache hit rate
- Enables chunked low-latency delivery: HTTP chunked transfer sends a chunk before the segment is done
- Pairs with Common Encryption; cbcs mode works for FairPlay, and on most modern Widevine/PlayReady devices

## video-latency | Where does live latency come from? How do you get from 30 s down to 2 s and to under 1 s?
- Glass-to-glass = capture + encode + ingest + packaging + CDN + segment buffering + player buffer
- Classic HLS: player holds ~3 segments of 6 s, so roughly 15-30 s
- LL-HLS: partial segments, blocking playlist reload, preload hints; LL-DASH: CMAF chunks; ~2-5 s
- WebRTC: sub-second via UDP and no segment buffer, but costlier to scale and fewer ABR features
- Trade-off: lower latency means smaller buffers, so more rebuffering risk and less CDN cacheability

## video-drm | How does DRM for streaming video work end to end?
- Content encrypted with AES under Common Encryption (cenc = CTR, cbcs = CBC pattern); one key per track
- Systems: Widevine (Google), FairPlay (Apple), PlayReady (Microsoft); multi-DRM packaging covers all devices
- Browser EME: init data (PSSH box) -> CDM generates license request -> license server checks auth and entitlement -> keys return to CDM
- Keys never reach JavaScript; decryption happens in the CDM
- Security levels: Widevine L1 (hardware TEE) vs L3 (software); HD and 4K usually need L1

## video-mse | How does a browser play adaptive streams? What do hls.js and Shaka do?
- Media Source Extensions: JS fetches segments and appends them to a SourceBuffer on a MediaSource
- Player library implements manifest parsing, ABR, buffer management, retries and EME licensing
- Must evict buffer behind the playhead (quota) and handle gaps, discontinuities and seeks
- Safari plays HLS natively; iPhone uses native HLS or ManagedMediaSource on recent iOS

## video-webrtc | How does a WebRTC call connect and stay stable?
- Signaling is app-defined (WebSocket/HTTP) and exchanges SDP offer/answer and ICE candidates
- ICE gathers host, STUN (server-reflexive) and TURN (relay) candidates; TURN is the fallback for strict NATs
- Media is RTP over UDP, encrypted with SRTP keyed through a DTLS handshake
- Congestion control (GCC with transport-wide feedback) adapts bitrate; NACK, FEC and PLI recover loss
- Opus for audio; data channels run over SCTP

## video-sfu | Mesh vs MCU vs SFU for group video. What are simulcast and SVC?
- Mesh: peer to peer, n-1 uplinks per client; breaks beyond about 4 people
- MCU: server decodes and mixes into one stream; light client, very expensive server CPU
- SFU: forwards packets without transcoding; one uplink per client, scales well
- Simulcast: sender uploads several resolutions and the SFU picks one per receiver
- SVC: one layered stream the SFU can thin out by dropping layers

## video-ingest | Which protocols carry live video from the encoder to the platform?
- RTMP: legacy TCP, H.264+AAC in FLV, limited codec support, still the common default for streamers
- SRT: UDP with ARQ retransmits, encryption and a tunable latency buffer; good over lossy links
- WebRTC ingest (WHIP): sub-second, browser-native, simple HTTP signaling
- Pipeline: ingest -> transcode ladder -> package (HLS/DASH) -> origin -> CDN -> player; redundant at each stage

## video-cdn | How do CDNs serve video well? What goes wrong at a live event start?
- Segments are immutable: long TTL and high hit rate; live manifests need TTL of about a segment or less
- Tiered caching and origin shield collapse many edge misses into one origin request
- Normalize cache keys (strip signed-token query params) and sign URLs for access control
- Thundering herd at start: request coalescing, pre-warming, manifest rate limits
- Multi-CDN with steering by performance and cost for resilience

## video-quality | How do you measure video quality and pick bitrates?
- PSNR (pixel error) and SSIM (structure) are cheap but correlate weakly with human perception
- VMAF (Netflix) fuses visual features with ML into a 0-100 score; about 6 points is roughly one just-noticeable difference
- Per-title encoding: per-content ladder instead of a fixed one; per-shot optimization goes further
- Pick rungs on the rate-quality convex hull across resolutions, so no rung is wasteful

## video-qoe | Which metrics tell you whether viewers have a good experience?
- Video start time (time to first frame) and video start failure rate
- Rebuffering ratio (stall time / watch time) and rebuffer count: the strongest drivers of abandonment
- Average delivered bitrate/resolution and number of quality switches
- Playback failure rate; segment by device, ISP, CDN and region to localize problems
- Startup fixes: low first rung, small initial segments, preconnect, prefetch manifest, HTTP/2 or HTTP/3

## video-timing | What are PTS, DTS and timebase? How do audio and video stay in sync?
- PTS = when to present a frame, DTS = when to decode it; they differ when B-frames reorder
- Timebase: MPEG-TS 90 kHz clock; MP4 has a timescale per track
- Audio usually acts as the master clock; video frames are dropped or repeated to match it
- Discontinuities (ad splices, restarts) need markers (EXT-X-DISCONTINUITY) and timestamp rebasing
- Variable frame rate (VFR) sources should be converted to constant frame rate before packaging

## video-hdr | What are SDR, HDR10, HLG and Dolby Vision?
- HDR needs 10-bit depth, wide gamut (BT.2020) and a transfer function such as PQ (ST 2084) or HLG
- HDR10: PQ with static metadata for the whole title; HDR10+ and Dolby Vision add dynamic per-scene metadata
- HLG: broadcast-oriented, backward compatible with SDR displays
- Delivery needs SDR fallback renditions and device capability detection

## video-audio | Which audio codecs do streaming services use?
- AAC-LC is the baseline (about 128 kbps stereo); HE-AAC for very low bitrates
- Opus: open, low latency, the WebRTC choice; handles speech and music
- AC-3/E-AC-3 for 5.1 surround; Dolby Atmos is carried in E-AC-3 JOC
- Audio renditions are separate tracks in the manifest (languages, descriptions), selected independently of video
- Loudness normalization to a target such as -23 LUFS (EBU R128)

## video-ffmpeg | Explain FFmpeg's pipeline and how you would build an HLS ladder with it.
- Pipeline: demux -> decode -> filter -> encode -> mux; -map selects streams, -c copy remuxes without re-encoding
- Per rendition: -vf scale, -c:v libx264 -crf N -maxrate -bufsize -preset
- Fixed keyframes: -g (frames) with -sc_threshold 0, or -force_key_frames, so renditions align
- HLS muxer: -f hls -hls_time 4 -hls_playlist_type vod -hls_segment_type fmp4; master playlist ties the variants
- Cutting with -c copy only works on keyframes; frame-accurate cuts need re-encode

## video-design-vod | Design a video-on-demand service like Netflix or YouTube at a high level.
- Upload to object storage, then a job queue drives parallel transcode (split into chunks, encode, merge) into the ladder
- Package as CMAF with HLS and DASH manifests, DRM applied if needed; store in object storage behind a CDN
- Metadata in a database (titles, renditions, ownership); search and recommendations as separate services
- Playback: client fetches manifest via an API with auth, then segments from the CDN; license server for DRM
- Scale and cost: CDN egress dominates, so per-title encoding, AV1 and ISP-embedded caches cut cost
- Reliability: idempotent transcode jobs with retries, multi-region storage, QoE monitoring

## video-design-live | Design a live streaming platform that reaches 1 million concurrent viewers.
- Ingest at regional points of presence (RTMP/SRT/WHIP), with a redundant encoder path
- Real-time transcoder ladder (GPU or hardware encoders), packager produces LL-HLS/CMAF segments to origin
- CDN with origin shield handles the fan-out; viewers never touch the origin directly
- Chat and presence as a separate scalable pub/sub service; live DVR window from segments kept in storage
- Capacity: bandwidth is viewers x bitrate (1M x 5 Mbps = 5 Tbps), so multi-CDN and pre-provisioning
- Failure handling: stream failover, manifest fallback, monitoring of QoE metrics in real time
