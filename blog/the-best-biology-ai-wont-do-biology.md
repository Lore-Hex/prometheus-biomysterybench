# The best biology AI won't do biology

Anthropic just shipped the strongest bioinformatics model anyone has built, and you can't use it for bioinformatics. The new Claude comes in two versions. Mythos 5 is the one that scores 83.9% on Anthropic's own BioMysteryBench, the best number they report, and it goes to vetted partners only. The version the rest of us can call is Fable 5, and Fable 5 blocks biology. Their system card says it flatly: send a chemistry-or-biology prompt to Fable 5 through the API and "the request is blocked, and the response returns a reason for the refusal." There is no fallback unless a developer opts into one. That is why every biology score in that system card belongs to Mythos. Fable doesn't have one. It won't sit still long enough to earn one.

So what does a working biologist actually do? I ran the experiment. BioMysteryBench hands a model a pile of raw, unlabeled biology — a crystal structure with the organism scrubbed out, a stack of sequencing reads, a set of ChIP peaks — and asks a plain question: what organism is this, what bacterium is in here, which transcription factor made these peaks. The model gets a shell and the usual bioinformatics tools and has to go figure it out. I rebuilt the public version of this as an open harness so anyone can rerun it and check me. Claude Opus 4.8, the best model you can actually call on TrustedRouter, scores the same 80.4% Anthropic published for it, so the harness isn't doing anything funny.

Then I pointed a stack of much cheaper models at the same three solvable tasks, some of them costing a fiftieth of what Opus costs, and watched.

| model | $/Mtok | solved | run cost |
|---|---:|:---:|---:|
| Claude Opus 4.8 | 19.80 | 3/3 | $1.57 |
| GLM-5.2 | 4.01 | 2/3 | $2.72 |
| Gemma-4-31B | 0.35 | 2/3 | $0.04 |
| DeepSeek-V4-Flash | 0.27 | 1/3 | $0.21 |
| DeepSeek-V3.2 | 0.45 | 1/3 | $0.31 |
| DeepSeek-V4-Pro | 0.84 | 1/3 | $1.03 |
| Kimi-K2.7-code | 3.56 | 1/3 | $1.12 |
| GLM-4.7-Flash | 0.38 | 0/3 | $0.70 |
| GPT-4o-mini | 0.54 | 0/3 | $0.10 |

Opus is the only model that got all three. It pulled the protein sequence out of the scrubbed structure, BLASTed it to *Homo sapiens*, named the bacterium, and ran MEME to find the transcription factor. On the longest, hardest task it finishes. It earns its 3/3.

But look at Gemma-4-31B. It got two of the three for four cents. It identified the human structure and named the bacterium — *Bacillus licheniformis*, correct — in six commands. GLM-5.2 also got two, and it's the one model under Opus that cracked the motif task every cheaper model gave up on. These are not models bluffing their way to an answer. They run real BLAST, they read the output, they do the work. A model that costs less than a vending-machine soda did most of what the twenty-dollar frontier model did.

The cheap ones know plenty of biology. What they run out of is patience. Seven of the nine got the structure-to-organism call, which is a one-shot lookup. They came apart on the task that takes twenty steps — pull the peaks, run MEME, read the motifs — where they wander until they hit the turn limit or the clock runs out. Half the misses in that table are a timer expiring, not a wrong answer. The thing the frontier model is really selling you is stamina on a long loop.

I also tried Fusion, the TrustedRouter mode that runs a whole panel of models and has a judge synthesize an answer at every step. On the two genuinely hard tasks I threw everything at it: one frontier model alone, an eight-model panel, and a hand-picked three of Gemma, GLM-5.2, and Opus. Every configuration scored zero out of two, and every one gave the same wrong answer — the same backwards drug condition on one task, the same "light stress" instead of heat on the other. That is the useful result. When every model in the world is confidently wrong in the same direction, a committee of them is just confidently wrong with better citations. Fusion is worth a lot when models disagree and one of them is right. It does nothing when they all share a blind spot.

The obvious question: Opus swept 3/3, so isn't it worth the money? Sometimes. For a twenty-step analysis where you need the loop to actually finish, pay for the model that finishes. For "what organism is this," you are paying fifty times over for a lookup a four-cent model gets right.

Three tasks is a small sample and you should treat it as a finger in the wind, not a leaderboard. The harness, the methodology, and every per-task result are open source. I don't publish the answer key.

The model that is best at biology is locked up. The model you can buy off the shelf won't touch biology. The thing that actually answers your question is one `--models` flag away, and sometimes it costs four cents. TrustedRouter routes you to whichever model will pick up the phone.
