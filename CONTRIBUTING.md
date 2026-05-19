# Contribución y mantenimiento — Asistente Lito

## Convención de versiones

Se usa [Semantic Versioning](https://semver.org/): `vMAJOR.MINOR.PATCH`

| Tipo | Cuándo usarlo | Ejemplo |
|------|--------------|---------|
| **patch** `x.x.N` | Bugfixes, ajustes menores, cambios que no alteran funcionalidad | `v2.5.0` → `v2.5.1` |
| **minor** `x.N.x` | Nueva funcionalidad compatible hacia atrás | `v2.5.0` → `v2.6.0` |
| **major** `N.x.x` | Cambios que rompen compatibilidad o refactors estructurales | `v2.5.0` → `v3.0.0` |

La versión activa del bot vive en la variable `VERSION` dentro de `bot.py`.

---

## Cómo hacer un commit

Desde el servidor, en `/home/ubuntu/asistente-lito/`:

```bash
./commit.sh "descripción del cambio"
```

El script ejecuta automáticamente:
1. `git add` de todos los archivos fuente del proyecto
2. `git commit -m "descripción"`
3. `git push origin main`

### Ejemplos de mensajes de commit

```bash
./commit.sh "fix: corregir cálculo de efectivo_neto cuando comisión es nula"
./commit.sh "feat: agregar herramienta listar_empresas al bot"
./commit.sh "refactor: extraer _build_image_prompt como función dinámica"
```

Formato recomendado: `tipo: descripción en minúscula` donde tipo es `fix`, `feat`, `refactor`, `docs`, `chore`.

---

## Regla de cierre de sesión

**Toda sesión de trabajo que incluya cambios en el código debe terminar con un commit.**

Antes de cerrar una sesión:

```bash
cd /home/ubuntu/asistente-lito
git status          # verificar qué cambió
./commit.sh "resumen de los cambios de esta sesión"
```

Esto garantiza que el repositorio en GitHub refleje siempre el estado real del servidor de producción.
