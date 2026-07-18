"""Políticas centrais do kernel do EP Market Hub."""

# Decisão de produto, não uma preferência do usuário. Mude este valor em uma
# versão validada para alterar quantos MT5/workers podem ficar ativos ao mesmo
# tempo. Cadastros de terminais continuam ilimitados.
MAX_ACTIVE_TERMINALS = 3
