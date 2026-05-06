import os
import sys

from dotenv import load_dotenv

from targets.openai_target import OpenAITarget


def main() -> int:
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is not set. Add OPENAI_API_KEY to .env "
            "(copy .env.example if needed) and try again.",
            file=sys.stderr,
        )
        return 1

    try:
        target = OpenAITarget()
        response = target.query("What is 2+2? Reply with just the number.")
    except Exception as e:
        print(f"Smoke test failed [{type(e).__name__}]: {e}", file=sys.stderr)
        return 1

    print(f"Response: {response}")
    print(f"Target name: {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
