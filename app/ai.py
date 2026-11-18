"""Sugestão de título, descrição e tags com Claude (Anthropic API).

Requer ANTHROPIC_API_KEY no .env (ou um perfil `ant auth login`).
"""

import anthropic
from pydantic import BaseModel

MODEL = "claude-opus-5-5"

SYSTEM = """Você é um estrategista de conteúdo para YouTube no Brasil. Gere metadados para um vídeo a partir do tema informado.

Regras:
- titles: exatamente 3 opções diferentes entre si, em português, com no máximo 90 caracteres, sem os caracteres < e >. Chamativos, mas sem prometer algo que o vídeo não entrega.
- description: em português, 2 a 4 parágrafos curtos. A primeira linha é um gancho (aparece na busca). Termine com 3 a 5 hashtags relevantes.
- tags: 8 a 15 termos de busca relevantes, sem o caractere #.
- Se for um Short, títulos mais curtos (até 60 caracteres) e descrição de 1 a 2 parágrafos."""


class Metadata(BaseModel):
    titles: list[str]
    description: str
    tags: list[str]


class AIError(RuntimeError):
    pass


def suggest(topic: str, notes: str = "", channel_title: str = "", is_short: bool = False) -> dict:
    prompt = f"Tema do vídeo: {topic.strip()}"
    if channel_title:
        prompt += f"\nCanal: {channel_title}"
    prompt += f"\nFormato: {'YouTube Short (vertical, até 60s)' if is_short else 'vídeo normal'}"
    if notes.strip():
        prompt += f"\nRoteiro ou anotações do autor:\n{notes.strip()[:6000]}"

    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=Metadata,
        )
    except anthropic.AuthenticationError as e:
        raise AIError("Chave da Anthropic inválida ou ausente. Configure ANTHROPIC_API_KEY no arquivo .env.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Limite de uso da API atingido. Tente de novo em instantes.") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Sem conexão com a API da Anthropic.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Erro da API da Anthropic ({e.status_code}).") from e
    except TypeError as e:  # credenciais ausentes: o SDK falha ao montar o header
        raise AIError("Configure ANTHROPIC_API_KEY no arquivo .env para usar a IA.") from e

    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise AIError("A IA não gerou sugestões para este tema.")

    out = response.parsed_output
    clean = lambda s, n: s.replace("<", "").replace(">", "").strip()[:n]
    return {
        "titles": [clean(t, 100) for t in out.titles if t.strip()][:3],
        "description": out.description.strip()[:5000],
        "tags": [t.strip().lstrip("#") for t in out.tags if t.strip()][:15],
    }
