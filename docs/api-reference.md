# Omada Open API — referência usada pelo projeto

Fonte: spec OpenAPI oficial (springdoc) publicada pela TP-Link em
`https://use1-omada-northbound.tplinkcloud.com/doc.html` (grupos `01`–`23`) e
validada contra o controller local **6.3.0.45** (`apiVer 3`).

> ⚠️ **Não baixe a spec do controller local** (`/v3/api-docs/...`). O controller gera
> o documento sob demanda e pedidos paralelos travaram o controller durante o
> desenvolvimento. Use sempre a cópia publicada na nuvem da TP-Link.

## Autenticação (modo Client)

| Passo | Chamada |
|---|---|
| omadacId | `GET /api/info` (público) → `result.omadacId` |
| Token | `POST /openapi/authorize/token?grant_type=client_credentials` body `{omadacId, client_id, client_secret}` |
| Refresh | `POST /openapi/authorize/token?grant_type=refresh_token&refresh_token=RT-…` body `{client_id, client_secret}` |
| Header | `Authorization: AccessToken=AT-…` |

Access token: 2 h. Refresh token: 14 dias.

Códigos de erro relevantes: `-44106` client id/secret inválido, `-44112` access token
expirado, `-44113` access token inválido, `-44114` refresh token expirado,
`-44116` autorização falhou, `-44205` device não suporta a feature,
`-39742` switch Agile Series deve usar o path `/switches/es/`.

Endpoints de lista (grid) exigem `page` (≥1) e `pageSize` (1–1000) e retornam
`{totalRows, currentPage, currentSize, data[]}`.

## Endpoints por requisito

Prefixo: `/openapi/v1/{omadacId}/sites/{siteId}`. MAC no formato `AA-BB-CC-DD-EE-FF`.

### Global
| Uso | Método / path | Campos |
|---|---|---|
| Sites | `GET /openapi/v1/{omadacId}/sites` | `siteId`, `name` |
| Devices | `GET …/devices` | `type` (gateway/switch/ap), `subtype` (smart/es), `name`, `model`, `status` (0 off,1 on,2 pending,3 heartbeat missed,4 isolated), `firmwareVersion`, `cpuUtil`, `memUtil`, `uptime`, `sn` |
| Reboot | `POST …/devices/{mac}/reboot` | — |

### Firewall (gateway)
| Uso | Método / path | Campos |
|---|---|---|
| CPU/Mem | `GET …/gateways/{mac}` | `cpuUtil`, `memUtil`, `temp` (nem todo modelo) |
| WANs | `GET …/gateways/{mac}/wan-status` | `port`, `name`, `mode` (0 WAN), `status`, `internetState`, `ip`, `proto`, `rxRate`/`txRate` (**KB/s**), `rx`/`tx` (bytes), `latency`, `loss` |
| ACLs | `GET …/acls/osg-acls` | `id`, `index`, `description`, `status` |
| ACL on/off | `PUT …/acls/osg-acls/{aclId}` | objeto completo (sem PATCH de status) |
| Policy routes | `GET …/routing/policy-routings` | `id`, `index`, `name`, `status` |
| Policy route on/off | `PUT …/routing/policy-routings/{id}` | objeto completo |
| WAN ativa (failover) | `GET …/internet/load-balance` | `linkBackup`, `primaryWans[]`/`backupWan` (ids `"<porta>_<uuid>"`), `mode` (0 = backup entra se qualquer primária cair, 1 = se todas caírem, outro = agendado) |
| Port forwarding | `GET …/nat/port-forwardings` | `id`, `name`, `status`, `externalPort`, `forwardIp`, `forwardPort` |
| Port forwarding on/off | `PUT …/nat/port-forwardings/{id}` | objeto completo (`PORT_FORWARD_PUT_FIELDS`) |
| Consumo por cliente (DPI) | `GET …/dashboard/clientTraffic?start&end&page&pageSize` | exige DPI ligado (`GET …/applicationControl/status` → `dpi`); vazio com DPI desligado |

> DPI: blocos de ~5 min; os lotes chegam ao controller **com atraso variável (observado ~35 min)**.
> Não há endpoint para forçar o envio (só `DELETE /stat/dpi`, que apaga). A integração recua em
> passos de 5 min (até 1 h) para a janela de 10 min mais recente com dados.

> `GET …/clients` (`activity`/`uploadActivity`) conta **todo** o tráfego do cliente, inclusive LAN
> (câmera → NVR). Não serve como "consumo de WAN".

**Taxas de WAN (`rxRate`/`txRate`):** rx = download, tx = upload (validado no ER605 com download
de ISO na WAN/LAN2: rx 118,8 / tx 1,2 Mbit/s). O controller **atualiza esses valores ~1×/min**
(média do minuto), então polling mais rápido que 60 s não traz informação nova.

**IP público por WAN:** a API não expõe. `publicIp` (device) e `sitePublicIp` (site)
trazem apenas o IP visto pelo controller local. Quando a WAN recebe IP global
(ex.: PPPoE) o próprio `ip` é o público; atrás de modem/CGNAT só há o IP privado.

### Switches
| Uso | Método / path | Campos |
|---|---|---|
| Info + portas (admin) | `GET …/switches/{mac}` (smart) / `GET …/switches/es/{mac}` (Agile) | `cpuUtil`, `memUtil`, `portList[]` → `port`, `name`, `status` (admin), `poeMode`, `lagPort`, `profileName` |
| PoE por porta (site) | `GET …/switches/ports/poe-info` | `switchMac`, `port`, `supportPoe` (capacidade real), `poe`, `power` (W), `voltage`, `current` |
| Estado das portas | `GET …/stat/switches/{mac}` | `supportPoe`, `ports[]` → `type` (1 cobre, 2 combo, 3 SFP), `portStatus.linkStatus`, `linkSpeed` (1 10M, 2 100M, 3 1G, 4 10G), `poe` (alimentando), `rx`/`tx` |
| Porta on/off (**como a UI do Omada**) | `PATCH …/switches/{mac}/ports/{port}` | `{name, profileId, profileOverrideEnable}` (só esses 3; override = valor atual da porta). off = profile built-in "Disable" (`type 1`, a UI também marca `disable: true`), on = profile memorizado ou "All" (`type 0`). Validado no SG2218P / 6.3.0.45. ⚠️ `PUT …/ports/{port}/profile` falha com `-39855` em portas com nome padrão (ex.: "Port2") |
| Estado completo das portas (site) | `GET …/switches/ports/overview` | `switchMac`, `port`, `portName`, `profileName`, `disable`, `profileOverrideEnable`, `connectedStatus` (0 ok, 2 disabled) |
| Profiles do site | `GET …/lan-profiles` | `id`, `name`, `type` (0 All, 1 Disable, 2 custom) |
| Flag legado de porta | `PUT …/switches/{mac}/ports/{port}/status` | `{status: 0|1}`; a UI não mostra como "Disabled". Só usado para limpar `status: 0` ao religar |
| PoE on/off | `PUT …/switches/{mac}/ports/{port}/poe-mode` | `{poeMode: 0|1}` |

### Access Points
| Uso | Método / path | Campos |
|---|---|---|
| Info | `GET …/aps/{mac}` | `cpuUtil`, `memUtil`, `wlanId` (grupo WLAN aplicado) |
| Grupos WLAN | `GET …/wireless-network/wlans` | `wlanId`, `name` |
| SSIDs | `GET …/wireless-network/wlans/{wlanId}/ssids` | `id`, `name`, `ssidEnable`, `band` (bitmask 1=2.4G 2=5G 4=6G), `broadcast`, `guestNetEnable`, `vlanId` |
| SSID on/off | `PATCH …/wireless-network/ssids/{ssidId}/enable` | `{ssidEnable: bool}` — **validar no 6.3** |

## Escritas validadas no controller real (6.3.0.45, 2026-11-16)

| Ação | Resultado |
|---|---|
| Porta on/off (SG2218P) via `PATCH …/ports/{port}` | ✅ igual à UI (profile Disable + `disable: true`) e restaura |
| Port forwarding on/off via `PUT` do objeto completo | ✅ só `status` muda; portas/IP/protocolo intactos |
| Policy route on/off via `PUT` do objeto completo | ✅ só `status` muda |
| PoE on/off (porta que segue o profile, tags 0/1) | ✅ off = `PATCH` override on + rede nativa/tags atuais + `poe: 0`; on = `PATCH` `poe: 1` com override, depois override off. Desligar só o override **mantém** o PoE desligado |
| PoE — o que não funciona no 6.3 | `PUT …/poe-mode` → `-39737` sem override; `PUT …/profile-override` → `-1001`; `PATCH` com `poe` e override off é aceito mas **ignorado** |
| PoE em porta com tags de VLAN personalizadas (trunk, `networkTagsSetting: 2`) | 🔒 a integração recusa (precisaria da lista de VLANs, que a API não devolve) |
| Porta on/off em Agile (ES206GP) | ✅ mesmo `PATCH …/switches/{mac}/ports/{port}` (sem `/es/`); voltou ao profile memorizado (vlan-Cameras) |
| PoE em Agile (ES206GP) | ✅ mesma sequência via override; câmera desligou e voltou em ~15 s |
| SSID on/off via `PATCH …/wireless-network/ssids/{id}/enable` | ✅ |
| ACL on/off via `PUT` do objeto completo | ✅ origem/destino/protocolo/index intactos |
| Reboot via `POST …/devices/{mac}/reboot` | ✅ sw3 reiniciou (uptime zerou) |
| Cliente: reconnect / block / unblock via `POST …/clients/{mac}/{acao}` | ✅ iPhone: reconectou (uptime 0), bloqueado (`insight/clients` → `block: true`), desbloqueado e voltou em ~10 s |
