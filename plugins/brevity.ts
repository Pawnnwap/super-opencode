/**
 * 📏 brevity — opencode plugin
 *
 * Drop in ~/.config/opencode/plugins/brevity.ts
 * Toggle on:  "brevity!"
 * Toggle off: "normal!"
 */

import type { Plugin } from "@opencode-ai/plugin";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const STATE_FILE = path.join(os.tmpdir(), "opencode-brevity-state.json");

function setBrevityState(active: boolean) {
  fs.writeFileSync(STATE_FILE, JSON.stringify({ active }));
}

function getBrevityState(): boolean {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf-8")).active;
  } catch {
    return false;
  }
}

// ─── brevity system prompt ───────────────────────────────────────────────────

const BREVITY_PROMPT = `
## BREVITY MODE ACTIVE 📏

You are a brevity expert. Talk short. Be brief.

RULES:
- Drop articles: no "a", "an", "the" in prose.
- No preamble/postamble: No "Sure!", "Hope this helps!".
- No structural transitions: No "In conclusion", "Additionally", "Moreover".
- No hedge: No "perhaps", "maybe", "I think".

NEVER change:
- Code blocks, technical terms, error messages, or Git commits.
`.trim();

// ─── aggressive post-processing with AI-ism stripping ────────────────────────

function stripRedundancy(text: string): string {
  const codeBlocks: string[] = [];
  // 1. Protect code blocks and inline code
  let processed = text.replace(/(```[\s\S]*?```|`[^`]*`)/g, (match) => {
    codeBlocks.push(match);
    return `__CODE_BLOCK_${codeBlocks.length - 1}__`;
  });

  const filters = [
    // 1. Articles
    /\b(a|an|the)\b/gi,

    // 2. Politeness / Conversational Filler
    /\b(Sure|Certainly|Great question|Happy to help|Absolutely|Of course)[.!]?\s*/gi,
    /\b(I hope this helps|Let me know if you have any questions|Feel free to ask|Thanks for asking|I'm here to help|Glad I could assist)[.!]?/gi,

    // 3. Unimportant Adverbs
    /\b(basically|actually|really|very|simply|just|totally|completely|absolutely|literally|honestly|clearly|obviously|essentially|specifically|virtually|highly|quite|fairly|rather|somewhat|slightly)\b/gi,

    // 4. Hedges and Softeners
    /\b(it might be worth|you could consider|perhaps|maybe|I think|I believe|it seems that|it appears that|likely|possibly|potentially)\b/gi,

    // 5. Semantic AI-isms (Structural Loops) 
    /\b(In conclusion|To summarize|In summary|Overall|To conclude|In short|In essence|To put it simply)\b,?\s*/gi,
    /\b(It's also worth mentioning that|Another thing to consider is|Additionally|Furthermore|Moreover|On another note|As a side note|It should be noted that)\b,?\s*/gi,
    /\b(Finally|Lastly|First of all|To start with|Next|Then)\b,?\s*/gi,

    // 6. Redundant Phrasing / Transition Filler
    /\b(The reason this is happening is because|In order to|Keep in mind that|It is important to note|Due to the fact that|With regard to|I would suggest|I would recommend|What you want to do is)\b/gi,
  ];

  filters.forEach(re => {
    processed = processed.replace(re, "");
  });

  // 7. Cleanup punctuation and whitespace
  processed = processed
    .replace(/\s\s+/g, " ")      // remove double spaces
    .replace(/\s([.,!?;:])/g, "$1") // fix spaces before punctuation
    .trim();

  // 8. Restore code blocks
  return processed.replace(/__CODE_BLOCK_(\d+)__/g, (_, i) => codeBlocks[parseInt(i)]);
}

// ─── trigger patterns ────────────────────────────────────────────────────────

const ON_PATTERNS = [/\bbrevity!\b/i, /\bbrevity\s+mode\b/i];
const OFF_PATTERNS = [/\bnormal!\b/i, /\bnormal\s+mode\b/i];

// ─── plugin export ───────────────────────────────────────────────────────────

export const tui: Plugin = async ({ client }) => {
  return {
    "tui.command.execute": async (input) => {
      const text = (input.text ?? "").trim();

      if (OFF_PATTERNS.some((p) => p.test(text))) {
        setBrevityState(false);
        await client.app.log({
          body: { service: "brevity", level: "info", message: "brevity mode OFF" },
        });
        return;
      }

      if (ON_PATTERNS.some((p) => p.test(text))) {
        setBrevityState(true);
        await client.app.log({
          body: { service: "brevity", level: "info", message: "brevity mode ON 📏" },
        });
      }
    }
  };
};

export const server: Plugin = async ({ client }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (!getBrevityState() || input.tool !== "ai") return;

      const existing: string = output.args?.systemPrompt ?? "";
      output.args.systemPrompt = existing ? `${existing}\n\n${BREVITY_PROMPT}` : BREVITY_PROMPT;
    },

    "tool.execute.after": async (input, output) => {
      if (!getBrevityState() || input.tool !== "ai") return;

      if (typeof output.result === "string") {
        output.result = stripRedundancy(output.result);
      }
    }
  };
};