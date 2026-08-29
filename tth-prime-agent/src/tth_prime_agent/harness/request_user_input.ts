import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const OptionSchema = Type.Object({
	label: Type.String(),
	value: Type.String(),
	description: Type.Optional(Type.String()),
});

const QuestionSchema = Type.Object({
	id: Type.String(),
	header: Type.Optional(Type.String()),
	question: Type.String(),
	options: Type.Array(OptionSchema),
	multiSelect: Type.Optional(Type.Boolean()),
	isOther: Type.Optional(Type.Boolean()),
	isSecret: Type.Optional(Type.Boolean()),
});

const Parameters = Type.Object({
	questions: Type.Array(QuestionSchema, { minItems: 1, maxItems: 3 }),
});

const HOST_QUESTION = "talktoharnesses/structured-question";
const HOST_ANSWER = "talktoharnesses/structured-answer";

export default function requestUserInput(pi: ExtensionAPI) {
	pi.registerTool({
		name: "request_user_input",
		label: "Request user input",
		description:
			"Ask the user one to three blocking clarification questions. Wait for the answers before continuing.",
		parameters: Parameters,

		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			if (!ctx.hasUI) {
				return {
					content: [{ type: "text" as const, text: "User input is unavailable." }],
					details: { cancelled: true },
				};
			}

			const answers: Record<string, string[]> = {};
			for (const question of params.questions) {
				const response = await ctx.ui.select(
					question.header ?? question.question,
					[JSON.stringify({ type: HOST_QUESTION, question })],
				);
				if (response === undefined) {
					return {
						content: [{ type: "text" as const, text: "User cancelled the questions." }],
						details: { cancelled: true },
					};
				}
				const decoded = JSON.parse(response) as { type?: string; values?: unknown };
				if (decoded.type !== HOST_ANSWER || !Array.isArray(decoded.values)) {
					throw new Error("Invalid TalkToHarnesses structured answer");
				}
				answers[question.id] = decoded.values.map(String);
			}

			return {
				content: [{ type: "text" as const, text: JSON.stringify({ answers }) }],
				details: { answers },
			};
		},
	});
}
