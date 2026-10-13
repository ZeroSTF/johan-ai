---
name: docker-trouble
description: A Docker container is down, crashing, restarting or not responding — find out why
triggers:
  - is my qdrant container running
  - a docker container keeps restarting
  - immich isn't loading
  - why did the postgres container stop
  - my container won't start
  - check the docker logs for errors
---
Containers on this box: johan-postgres, johan-qdrant, johan-searxng (Johan's own) and the
immich_* stack. Use run_command:
1. State of all of them: `docker ps -a --format "{{.Names}}  {{.Status}}"`
   ("Restarting", "Exited (1)", "unhealthy" are the interesting ones; "Paused" usually
   means johan-pause/GameMode paused it — that is expected during a game).
2. Why: `docker logs --tail 50 NAME` (errors are usually at the end; page with
   run_command(page=N) if long). Exit details:
   `docker inspect --format "{{.State.Status}} {{.State.ExitCode}} {{.State.Error}} {{.State.OOMKilled}}" NAME`.
3. Common causes: OOMKilled true → hit its memory limit; port already in use → `ss -ltnp`;
   disk full → `df -h /`; the Docker daemon itself → `systemctl status docker --no-pager`.
Fix: propose_action `docker start NAME` / `docker restart NAME` / `docker unpause NAME`
(he approves in F4). Never remove containers or volumes — that is his data (immich photos,
Johan's database). Changing a compose file is his call; show him the line.
