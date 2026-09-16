# DEV-008B.1B — auditoria Codex 1

## Resultado

Implementação do Claude auditada e corrigida pelo Codex, sem dados reais,
MT5 ou rede. A execução real permanece não autorizada.

## Correções principais

- teste genérico deixou de usar por engano a identidade fixa do MVP WIN;
- timestamps dos ticks UTC reais são convertidos para o relógio da sessão
  exigido pelo núcleo causal;
- M1 passou a ser imutável e endereçado por conteúdo;
- estado e manifesto Atlas passaram a viver na mesma geração, promovida por
  `current.json` como único ponto de commit;
- lock exclusivo foi adicionado para runs reais;
- estado, objetos e manifesto vigentes são validados antes de `no_change`;
- mudança de manifesto/parametrização cria nova geração e não reutiliza
  estado incompatível;
- `read_session_ticks_to_m1` exige sempre um validador de identidade.

## Validação

- 80 testes dirigidos (adaptador, MVP e materializador): aprovados;
- suíte completa do repositório: aprovada;
- Ruff, `compileall` e `git diff --check`: aprovados;
- manifesto local `WINV26` validado estruturalmente, com 12 sessões
  descobertas entre 28/08 e 15/09/2026; ticks ainda não processados.

## Veredito

Apta para `dry-run` real do adaptador `WINV26`. Commit/push, execução real
com escrita e materialização no Atlas permanecem portões separados.
