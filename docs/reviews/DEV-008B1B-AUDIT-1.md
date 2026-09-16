# DEV-008B.1B — auditoria Codex 1

## Resultado

Implementação do Claude auditada e corrigida pelo Codex, sem dados reais,
MT5 ou rede. A execução real permanece não autorizada.

## Correções principais

- teste genérico deixou de usar por engano a identidade fixa do MVP WIN;
- relógio entregue pelo produtor MT5 é preservado sem conversão adicional,
  conforme a mesma convenção das séries M1 históricas do Atlas;
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

## Correção pós-execução real

A inspeção do primeiro M1 real revelou um deslocamento indevido de três
horas: o produtor MT5 já fornece o relógio da fonte tipado como UTC. A
política foi corrigida para preservar esse valor, a versão do produtor e o
fingerprint foram alterados para invalidar automaticamente a geração
incorreta, sem apagá-la do histórico imutável. A reconstrução promoveu a
geração `20260916T192118686687_633cd170`, recalculou as 11 sessões elegíveis
e preservou horários de fonte entre 09:02/09:03 e 18:31/19:30. A repetição
resultou em `no_change`, sem alterar bytes ou mtime do ponteiro vigente.

## Veredito

Apta para `dry-run` real do adaptador `WINV26`. Commit/push, execução real
com escrita e materialização no Atlas permanecem portões separados.
