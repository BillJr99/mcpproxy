import traceback
from typing import Any


async def request_text_input_with_fallback(
    context: dict[str, Any],
    field_name: str,
    message: str,
    description: str,
) -> dict[str, Any]:
    try:
        ctx = context.get("mcp_context")

        if ctx is not None and hasattr(ctx, "elicit"):
            try:
                result = await ctx.elicit(
                    message=message,
                    schema={
                        "type": "object",
                        "properties": {
                            field_name: {
                                "type": "string",
                                "description": description,
                            }
                        },
                        "required": [field_name],
                    },
                )

                if isinstance(result, dict):
                    value = result.get(field_name)
                else:
                    value = getattr(result, field_name, None)

                if value:
                    return {
                        "ok": True,
                        "value": value,
                        "source": "elicitation",
                    }
            except Exception as e:
                print(f"request_text_input_with_fallback elicitation error: {e}")
                traceback.print_exc()

        return {
            "ok": False,
            "needs_input": True,
            "input": {
                "name": field_name,
                "description": description,
                "type": "string",
            },
            "message": message,
        }

    except Exception as e:
        print(f"request_text_input_with_fallback error: {e}")
        traceback.print_exc()
        return {
            "ok": False,
            "error": str(e),
        }
