from __future__ import annotations
import os, json, io, csv
import re
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Flask, request, render_template, redirect, url_for, session, send_from_directory, send_file, flash, Response, abort
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import sqlite3
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

# -----------------------------------------------------------------------------
# Configuração
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "base_dados.db"
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
TEMPLATES_DIR = BASE_DIR / "templates"
OVERWRITE_TEMPLATES = False
APP_TITLE = "Registo Limpezas de Viaturas Grupo Tejo"
APP_SIGNATURE = "Created by Pedro Fonte"

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf"}

TZ_PT = ZoneInfo("Europe/Lisbon")


def now_pt() -> datetime:
    """Data/hora atual em Portugal continental (WET/WEST automático)."""
    return datetime.now(TZ_PT)


def today_pt() -> date:
    return now_pt().date()


def now_pt_iso() -> str:
    """ISO sem timezone para gravar na BD (hora civil de Portugal)."""
    return now_pt().replace(tzinfo=None).isoformat(timespec="seconds")


def today_pt_iso() -> str:
    return today_pt().isoformat()


def parse_descricao_viaturas(value: str | None) -> list[str]:
    """
    Aceita uma lista separada por vírgulas ou ponto-e-vírgula.
    Ex.: "Autocarro Urbano;Autocarro Suburbano"
    """
    parts = re.split(r"[;,]", value or "")
    return [p.strip() for p in parts if p and p.strip()]


def sql_date_eq_today(col: str, conn) -> tuple[str, str]:
    ph = sql_placeholder(conn)
    return f"date({col}) = {ph}", today_pt_iso()


def _gestor_ultimos_registos_verificacao(cur, ph, regiao_gestor: str | None, *, apenas_pendentes: bool):
    """Último registo concluído por viatura+protocolo, filtrado por região e estado de verificação."""
    where = ["r.estado='concluido'"]
    params: list[str] = []
    if regiao_gestor:
        where.append(f"v.regiao = {ph}")
        params.append(regiao_gestor)
    if apenas_pendentes:
        where.append("(r.verificacao_limpeza IS NULL OR TRIM(r.verificacao_limpeza)='')")
    else:
        where.append("(r.verificacao_limpeza IS NOT NULL AND TRIM(r.verificacao_limpeza)<>'')")

    subquery = f"""
        SELECT r.viatura_id, r.protocolo_id, MAX(r.data_hora) AS ult
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {" AND ".join(where)}
        GROUP BY r.viatura_id, r.protocolo_id
    """
    cur.execute(
        f"""
        WITH base AS (
            {subquery}
        )
        SELECT
            r.id AS registo_id,
            v.matricula,
            v.num_frota,
            v.descricao,
            p.nome AS protocolo_nome,
            r.data_hora,
            r.local,
            r.verificacao_limpeza,
            r.comentarios_verificacao,
            r.verificacao_em
        FROM registos_limpeza r
        JOIN base b
          ON b.viatura_id = r.viatura_id
         AND b.protocolo_id = r.protocolo_id
         AND b.ult = r.data_hora
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        ORDER BY p.nome, v.matricula
        """,
        params,
    )
    return [dict(x) for x in cur.fetchall()]


def _processar_verificacoes_gestor(cur, ph, selected_ids: list[str]) -> list[str]:
    erros: list[str] = []
    for rid_str in selected_ids:
        try:
            rid = int(rid_str)
        except (TypeError, ValueError):
            continue
        status = (request.form.get(f"status_{rid_str}") or "").strip()
        comentario = (request.form.get(f"coment_{rid_str}") or "").strip()
        if not status:
            erros.append(f"Registo {rid}: falta o estado da verificação.")
            continue
        status_l = status.lower()
        if status_l in {"não conforme", "nao conforme"} and not comentario:
            erros.append(f"Registo {rid}: comentário obrigatório para 'não conforme'.")
            continue
        comentarios_to_save = comentario if status_l in {"não conforme", "nao conforme"} else None
        cur.execute(
            f"""UPDATE registos_limpeza
                SET verificacao_limpeza={ph},
                    comentarios_verificacao={ph},
                    verificacao_em={ph}
                WHERE id={ph}""",
            (status, comentarios_to_save, now_pt_iso(), rid),
        )
    return erros


app = Flask(__name__, template_folder=str(BASE_DIR))
app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-key-please-change")

UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)

print("### DB em uso:", DB_PATH)


# -----------------------------------------------------------------------------
# Templates auto-criados (para ser plug-and-play)
# -----------------------------------------------------------------------------
def write_templates():
    files: dict[str, str] = {}
    
    files["home.html"] = """{% extends "base.html" %}
{% block content %}
<div id="protoModal" style="display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,.3);z-index:999;">
  <div style="background:#fff;padding:24px;max-width:600px;margin:60px auto;border-radius:8px;box-shadow:0 2px 12px #0002;">
    <h3>Viaturas limpas por protocolo</h3>
    <div id="protoModalContent"></div>
    <button class="btn" onclick="document.getElementById('protoModal').style.display='none'">Fechar</button>
  </div>
</div>
  <h2>Dashboard</h2>

{% set CH = charts %}
<!-- KPI Cards -->
<div class="kpis" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:.5rem 0 1rem;">
  <div class="card" style="padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;">
    <div class="muted" style="font-size:.85rem;color:#6b7280;">Registos hoje</div>
    <div style="font-size:1.6rem;font-weight:700;">{{ CH.kpi_today }}</div>
  </div>
  <div class="card" style="padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;">
    <div class="muted" style="font-size:.85rem;color:#6b7280;">Registos últimos 7 dias</div>
    <div style="font-size:1.6rem;font-weight:700;">{{ CH.kpi_week }}</div>
  </div>
  <div class="card" style="padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;">
    <div class="muted" style="font-size:.85rem;color:#6b7280;">Registos este mês</div>
    <div style="font-size:1.6rem;font-weight:700;">{{ CH.kpi_month }}</div>
  </div>
  <div class="card" style="padding:10px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;">
    <div class="muted" style="font-size:.85rem;color:#6b7280;">Viaturas limpas hoje</div>
    <div style="font-size:1.6rem;font-weight:700;">{{ CH.kpi_today_veh }}</div>
  </div>
</div>
  
  {% set CH = charts %}
    <div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
      <div class="card">
    <h3 style="margin:0 0 .5rem">Viaturas distintas por protocolo</h3>
    <canvas id="chart_proto" height="90"></canvas>
    <ul class="muted" style="display:flex;flex-wrap:wrap;gap:.75rem;margin:.5rem 0 0;padding:0;list-style:none">
      {% for i in range(CH.proto_labels|length) %}
        <li>{{ CH.proto_labels[i] }}: <b>{{ CH.proto_values[i] }}</b></li>
      {% endfor %}
    </ul>
    <div style="text-align:right;margin-top:.5rem;">
      <button class="btn btn-primary" type="button" onclick="showProtoModal();event.stopPropagation();">Ver lista</button>
    </div>
  </div>

    <div class="card">
      <h3 style="margin:0 0 .5rem">Média de dias desde última limpeza</h3>
      <canvas id="chart_avg_days" height="90"></canvas>
      <div class="muted">Frota: <b>{{ CH.fleet_size }}</b> — Média: <b>{{ CH.avg_days }}</b> dias</div>
    </div>
    
    <div class="card">
      <h3 style="margin:0 0 .5rem">Limpezas por local</h3>
      <canvas id="chart_local" height="90"></canvas>
      <ul class="muted" style="display:flex;flex-wrap:wrap;gap:.75rem;margin:.5rem 0 0;padding:0;list-style:none">
        {% for i in range(CH.local_labels|length) %}
          <li>{{ CH.local_labels[i] }}: <b>{{ CH.local_values[i] }}</b></li>
        {% endfor %}
      </ul>
    </div>

    <div class="card">
      <h3 style="margin:0 0 .5rem">Limpezas por funcionário</h3>
      <canvas id="chart_func" height="90"></canvas>
      <ul class="muted" style="display:flex;flex-wrap:wrap;gap:.75rem;margin:.5rem 0 0;padding:0;list-style:none">
        {% for i in range(CH.func_labels|length) %}
          <li>{{ CH.func_labels[i] }}: <b>{{ CH.func_values[i] }}</b></li>
        {% endfor %}
      </ul>
    </div>
  </div>

  {% raw %}
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    const CH = {{ charts | tojson }};
    const viaturas = {{ viaturas | tojson }};
    const protocolos = {{ protocolos | tojson }};

    function bar(id, labels, data, title){
      const ctx = document.getElementById(id).getContext('2d');
      const valueLabel = {
        id: 'valueLabel',
        afterDatasetsDraw(chart, args, pluginOptions) {
          const {ctx, chartArea: {top}, scales: {x, y}} = chart;
          ctx.save();
          ctx.font = '12px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillStyle = '#111';
          chart.data.datasets.forEach((dataset, i) => {
            const meta = chart.getDatasetMeta(i);
            meta.data.forEach((bar, index) => {
              const val = dataset.data[index];
              if (val == null) return;
              const posY = Math.min(bar.y, y.getPixelForValue(val)) - 4;
              ctx.fillText(String(val), bar.x, posY);
            });
          });
          ctx.restore();
        }
      };
      new Chart(ctx, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: title, data: data }] },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
        },
        plugins: [valueLabel]
      });
    }

    bar('chart_proto', CH.proto_labels, CH.proto_values, '');
    (function(){
      const ctx = document.getElementById('chart_avg_days').getContext('2d');
      const valueLabel = {
        id: 'valueLabel',
        afterDatasetsDraw(chart, args, pluginOptions) {
          const {ctx, chartArea: {top}, scales: {x, y}} = chart;
          ctx.save();
          ctx.font = '12px system-ui, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillStyle = '#111';
          chart.data.datasets.forEach((dataset, i) => {
            const meta = chart.getDatasetMeta(i);
            meta.data.forEach((bar, index) => {
              const val = dataset.data[index];
              if (val == null) return;
              const posY = Math.min(bar.y, y.getPixelForValue(val)) - 4;
              ctx.fillText(String(val), bar.x, posY);
            });
          });
          ctx.restore();
        }
      };
      new Chart(ctx, {
        type: 'bar',
        data: { labels: ['Média'], datasets: [{ label: 'Dias', data: [CH.avg_days] }] },
        options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } },
        plugins: [valueLabel]
      });
    })();
    bar('chart_local', CH.local_labels, CH.local_values, '');
    bar('chart_func', CH.func_labels, CH.func_values, '');

    function showProtoModal() {
      let html = "";
      protocolos.forEach(p => {
        html += `<h4>${p.nome}</h4><ul>`;
        viaturas.forEach(v => {
          // Aqui pode filtrar as viaturas limpas por protocolo, se tiver esse dado
          html += `<li>${v.matricula} ${v.descricao ? "— " + v.descricao : ""}</li>`;
        });
        html += "</ul>";
      });
      document.getElementById("protoModalContent").innerHTML = html;
      document.getElementById("protoModal").style.display = "block";
    }
  </script>
  {% endraw %}
  {% endblock %}
"""



    files["login.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Login</h2>
  <form method="post" class="card" style="max-width:480px;">
  <div class="row">
    <label>Utilizador
      <input type="text" name="username" required>
    </label>
    <label>Password
      <input type="password" name="password" required>
    </label>
  </div>
  <button class="btn btn-primary" type="submit">Entrar</button>
  </form>
{% endblock %}
"""

    files["403.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Acesso negado</h2>
  <p>Não tem permissões para aceder a esta página.</p>
  <p>
    <a class="btn" href="{{ url_for('home') }}">Ir para o início</a>
    {% if can('registos:view') %}<a class="btn" href="{{ url_for('registos') }}">Ver registos</a>{% endif %}
  </p>
{% endblock %}
"""

    files["protocolos.html"] = """{% extends "base.html" %}
{% block content %}
   <h2>Protocolos</h2>
   {% if can('protocolos:edit') %}
     <p><a class="btn btn-primary" href="{{ url_for('protocolo_novo') }}">Novo Protocolo</a></p>
   {% endif %}
   <table>
     <thead>
       <tr>
         <th>Nome</th>
         <th>Passos</th>
         <th>Frequência (dias)</th>
         <th>Ativo</th>
        {% if can('protocolos:edit') %}<th style="width:200px;">Ações</th>{% endif %}
       </tr>
     </thead>
     <tbody>
       {% for p in protocolos %}
       <tr>
         <td>{{ p.nome }}</td>
         <td>
          {% set data = p.passos_json|loadjson %}
          {% if data and data.passos %}
            <ol>
              {% for step in data.passos %}
                <li>{{ step }}</li>
              {% endfor %}
            </ol>
          {% else %}
            <span class="muted">sem passos</span>
          {% endif %}
        </td>
        <td>{{ p.frequencia_dias or "—" }}</td>
        <td>{{ "Sim" if p.ativo==1 else "Não" }}</td>
        {% if can('protocolos:edit') %}
        <td>
          <a class="btn" href="{{ url_for('protocolo_editar', pid=p.id) }}">Editar</a>
          <form method="post" action="{{ url_for('protocolo_apagar', pid=p.id) }}"
                onsubmit="return confirm('Apagar protocolo {{ p.nome }}?');"
                style="display:inline-block;margin-left:6px;">
            <button class="btn btn-danger" type="submit">Apagar</button>
          </form>
        </td>
        {% endif %}
       </tr>
       {% endfor %}
     </tbody>
   </table>
{% endblock %}
"""


    files["protocolos_form.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>{% if modo == 'novo' %}Novo Protocolo{% else %}Editar Protocolo{% endif %}</h2>
  <form method="post">
    <div class="row">
      <label>Nome <input type="text" name="nome" value="{{ form.nome }}" required></label>
      <label>Frequência (dias) <input type="number" name="frequencia_dias" min="0" step="1" value="{{ form.frequencia_dias }}"></label>
      <label>Ativo
        <select name="ativo">
          <option value="1" {% if form.ativo == 1 %}selected{% endif %}>Sim</option>
          <option value="0" {% if form.ativo != 1 %}selected{% endif %}>Não</option>
        </select>
      </label>
    </div>
    <div class="row" style="grid-template-columns: 1fr;">
      <label>Passos (um por linha)
        <textarea name="passos" rows="10" placeholder="Inspeção interior
        Aspirar
        Desinfetar superfícies">{{ form.passos }}</textarea>
      </label>
    </div>
    <p>
      <button class="btn btn-primary" type="submit">{% if modo == 'novo' %}Criar{% else %}Guardar{% endif %}</button>
      <a class="btn" href="{{ url_for('protocolos') }}">Cancelar</a>
    </p>
  </form>
{% endblock %}
"""

    files["anexos.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Anexos do registo #{{ registo_id }}</h2>
  {% if anexos %}
    <ul>
      {% for a in anexos %}
        <li><a href="{{ url_for('download_anexo', anexo_id=a.id) }}">{{ a.caminho }}</a> <span class="muted">({{ a.tipo }})</span></li>
      {% endfor %}
    </ul>
  {% else %}
    <div class="card">Sem anexos.</div>
  {% endif %}
{% endblock %}
"""

    files["admin.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Administração</h2>
  <ul>
    {% if can('users:manage') %}<li><a href="{{ url_for('admin_users') }}">Utilizadores</a></li>{% endif %}
    {% if can('roles:manage') %}<li><a href="{{ url_for('admin_roles') }}">Perfis (roles)</a></li>{% endif %}
    {% if can('viaturas:import') %}<li><a href="{{ url_for('admin_import_viaturas') }}">Importar viaturas (CSV)</a></li>{% endif %}
    {% if can('protocolos:view') %}<li><a href="{{ url_for('protocolos') }}">Protocolos</a></li>{% endif %}
  </ul>
{% endblock %}
"""

    files["admin_users.html"] = """{% extends "base.html" %}
+{% block content %}
+  <h2>Utilizadores</h2>
+  <p><a class="btn btn-primary" href="{{ url_for('admin_user_new') }}">Novo utilizador</a></p>
+  <table>
+    <thead>
+      <tr>
+        <th>Username</th>
+        <th>Nome</th>
+        <th>Perfil</th>
+        <th>Região</th>
+        <th>Ativo</th>
+        <th>Criado</th>
+        <th style="width:240px;">Ações</th>
+      </tr>
+    </thead>
+    <tbody>
+      {% for u in users %}
+      <tr>
+        <td>{{ u.username }}</td>
+        <td>{{ u.nome or "—" }}</td>
+        <td>{{ u.role }}</td>
+        <td>{{ u.regiao or "—" }}</td>
+        <td>{{ "Sim" if u.ativo==1 else "Não" }}</td>
+        <td>{{ u.criado_em }}</td>
+        <td>
+          <a class="btn" href="{{ url_for('admin_user_edit', user_id=u.id) }}">Editar</a>
+          <form method="post" action="{{ url_for('admin_user_toggle', user_id=u.id) }}" style="display:inline-block;margin-left:6px;">
+            <button class="btn" type="submit">{% if u.ativo==1 %}Desativar{% else %}Ativar{% endif %}</button>
+          </form>
+          <form method="post" action="{{ url_for('admin_user_delete', user_id=u.id) }}"
+                onsubmit="return confirm('Eliminar {{ u.username }}? Esta ação é definitiva.');"
+                style="display:inline-block;margin-left:6px;">
+            <button class="btn btn-danger" type="submit"
+                    {% if u.username == session['username'] %}disabled{% endif %}>Apagar</button>
+          </form>
+        </td>
+      </tr>
+      {% endfor %}
+    </tbody>
+  </table>
+{% endblock %}
+"""

    files["admin_user_form.html"] = """{% extends "base.html" %}
 {% block content %}
   <h2>Novo Utilizador</h2>
   <form method="post">
     <div class="row">
       <label>Username <input name="username" required></label>
       <label>Nome <input name="nome" placeholder="opcional"></label>
      <label>Região <input name="regiao" placeholder="ex.: Região Norte"></label>
       <label>Password <input name="password" type="password" required></label>
       <label>Perfil
         <select name="role" required>
           {% for r in roles %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
         </select>
      </label>
      <label>Ativo
        <select name="ativo">
          <option value="1" selected>Sim</option>
          <option value="0">Não</option>
        </select>
      </label>
    </div>
    <p><button class="btn btn-primary" type="submit">Criar</button>
       <a class="btn" href="{{ url_for('admin_users') }}">Cancelar</a></p>
  </form>
{% endblock %}
"""

    files["admin_roles.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Perfis (roles)</h2>
  <p><a class="btn btn-primary" href="{{ url_for('admin_role_new') }}">Novo perfil</a></p>

  <h3>Perfis base</h3>
  <ul>{% for r in base_roles %}<li>{{ r }} <span class="muted">(predefinido)</span></li>{% endfor %}</ul>

  <h3>Perfis em BD</h3>
  {% if db_roles %}
    <ul>{% for r in db_roles %}<li>{{ r }}</li>{% endfor %}</ul>
  {% else %}
    <div class="card">Ainda não existem perfis personalizados.</div>
  {% endif %}
{% endblock %}
"""

    files["admin_role_form.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Novo Perfil</h2>
  <form method="post">
    <div class="row">
      <label>Nome do perfil (minúsculas) <input name="name" required></label>
    </div>
    <div class="row" style="grid-template-columns: 1fr;">
      <fieldset class="card">
        <legend>Permissões</legend>
        {% for p in perms %}
          <label style="display:block;margin:.25rem 0;">
            <input type="checkbox" name="perms" value="{{ p }}"> {{ p }}
          </label>
        {% endfor %}
      </fieldset>
    </div>
    <p><button class="btn btn-primary" type="submit">Criar</button>
       <a class="btn" href="{{ url_for('admin_roles') }}">Cancelar</a></p>
  </form>
{% endblock %}
"""

    files["admin_import_viaturas.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Importar Viaturas (CSV)</h2>
  <p class="muted">Formato recomendado (cabeçalho): <code>matricula,descricao,filial,num_frota,ativo</code></p>
  <form method="post" enctype="multipart/form-data">
    <div class="row">
      <label>Ficheiro CSV <input type="file" name="ficheiro" accept=".csv" required></label>
    </div>
    <p><button class="btn btn-primary" type="submit">Importar</button>
       <a class="btn" href="{{ url_for('admin_panel') }}">Cancelar</a></p>
  </form>
{% endblock %}
"""
    files["registos.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Registos de Limpeza</h2>
  {% if can('registos:create') %}
  <p><a class="btn btn-primary" href="{{ url_for('novo_registo') }}">Novo Registo</a></p>
  {% endif %}
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Data/Hora</th>
        <th>Matrícula</th>
        <th>Protocolo</th>
        <th>Operador</th>
        <th>Local</th>
        <th style="width:160px;">Ações</th>
      </tr>
    </thead>
    <tbody>
      {% for r in registos %}
      <tr>
        <td>{{ r.registo_id }}</td>
        <td>{{ r.data_hora }}</td>
        <td>{{ r.matricula }}</td>
        <td>{{ r.protocolo }}</td>
        <td>{{ r.funcionario }}</td>
        <td>{{ r.local }}</td>
        <td>
          <a class="btn" href="{{ url_for('registo_detalhe', rid=r.registo_id) }}">Ver</a>
          {% if can('registos:delete') %}
          <form method="post" action="{{ url_for('registo_apagar', rid=r.registo_id) }}"
                onsubmit="return confirm('Apagar registo #{{ r.registo_id }}?');"
                style="display:inline-block;margin-left:6px;">
            <button class="btn btn-danger" type="submit">Apagar</button>
          </form>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
"""
    files["novo_registo.html"] = """{% extends "base.html" %}
{% block content %}
  <h2>Novo Registo</h2>
  <form method="post" enctype="multipart/form-data">
    <div class="row">
      <label>Viatura
        <select name="viatura_id" required>
          <option value="">— selecione —</option>
          {% for v in viaturas %}
            <option value="{{ v.id }}">{{ v.matricula }}{% if v.num_frota %} — {{ v.num_frota }}{% endif %}{% if limpa_hoje_map[v.id] %} ★{% endif %}</option>
          {% endfor %}
        </select>
      </label>
      <label>Protocolo
        <select name="protocolo_id" required>
          <option value="">— selecione —</option>
          {% for p in protocolos %}<option value="{{ p.id }}">{{ p.nome }}</option>{% endfor %}
        </select>
      </label>
      <label>Estado
        <select name="estado">
          <option value="concluido">Concluído</option>
          <option value="em_progresso">Em progresso</option>
        </select>
      </label>
    </div>
    <div class="row">
      <label>Local <input name="local" placeholder="p.ex.: Parque A"></label>
      <label>Hora Início <input name="hora_inicio" placeholder="HH:MM"></label>
      <label>Hora Fim <input name="hora_fim" placeholder="HH:MM"></label>
    </div>
    <div class="row" style="grid-template-columns:1fr;">
      <label>Observações
        <textarea name="observacoes" rows="3"></textarea>
      </label>
    </div>
    <div class="row" style="align-items:center;">
      <label><input type="checkbox" name="extra_autorizada" value="1"> Limpeza extra autorizada (segunda limpeza no mesmo dia)</label>
      <label>Responsável <input name="responsavel_autorizacao" placeholder="nome do responsável"></label>
    </div>
    <div class="row">
      <label>Ficheiros (opcional) <input type="file" name="ficheiros" multiple></label>
    </div>
    <p>
      <button class="btn btn-primary" type="submit">Criar</button>
      <a class="btn" href="{{ url_for('registos') }}">Cancelar</a>
    </p>
  </form>
{% endblock %}
"""
    for name, content in files.items():
        path = TEMPLATES_DIR / name
        if OVERWRITE_TEMPLATES or not path.exists():
            path.write_text(content, encoding="utf-8")

    

# -----------------------------------------------------------------------------
# Helpers / filtros
# -----------------------------------------------------------------------------
def get_conn():
    db_url = os.environ.get("DATABASE_URL")
    if db_url and psycopg2:
        # Heroku/PostgreSQL
        conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        # Local/SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def is_postgres(conn):
    return hasattr(conn, "server_version")  # True para psycopg2, False para sqlite3

def sql_placeholder(conn):
    return "%s" if is_postgres(conn) else "?"

def table_columns(conn, table_name: str) -> set[str]:
    cur = conn.cursor()
    if is_postgres(conn):
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        )
        return {r["column_name"] for r in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table_name})")
    return {r["name"] for r in cur.fetchall()}

def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTS

@app.template_filter("loadjson")
def _filter_loadjson(value):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

# -----------------------------------------------------------------------------
# RBAC (com perfis dinâmicos em BD)
# -----------------------------------------------------------------------------
PERMISSIONS = {
    "admin": {"*"},
    "gestor": {
        "dashboard:view","viaturas:view","protocolos:view","protocolos:edit",
        "registos:view","registos:create","registos:edit","export:excel",
        "viaturas:import","users:manage","roles:manage","admin:panel"
    },
    "operador": {"viaturas:view","protocolos:view","registos:view","registos:create","registos:edit","export:excel"},
    "leitura":  {"dashboard:view","viaturas:view","protocolos:view","registos:view"},
}
KNOWN_PERMS = sorted({p for perms in PERMISSIONS.values() for p in perms if p != "*"})

def normalize_role(role: str) -> str:
    r = (role or "leitura").lower().strip()
    return r if r in PERMISSIONS else "leitura"

def get_db_role_perms(role: str) -> set[str]:
    role = (role or "").strip().lower()
    if not role:
        return set()
    conn = get_conn()
    ph = sql_placeholder(conn)
    cur = conn.cursor()
    cur.execute(f"SELECT id FROM roles WHERE LOWER(name)={ph}", (role,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return set()
    cur.execute(f"SELECT perm FROM role_permissions WHERE role_id={ph}", (r["id"],))
    perms = {row["perm"] for row in cur.fetchall()}
    conn.close()
    return perms

def has_perm(role: str, perm: str) -> bool:
    role = normalize_role(role)
    perms = PERMISSIONS.get(role, set())
    if "*" in perms: return True
    if perm in perms: return True
    if ":" in perm and (perm.split(":",1)[0] + ":*") in perms: return True
    return False

def user_can(perm: str) -> bool:
    return has_perm(normalize_role(session.get("role")), perm)

def require_perm(perm: str):
    from functools import wraps
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            if not user_can(perm):
                flash("Sem permissões para esta ação.", "danger")
                return redirect(url_for("sem_permissao"))
            return fn(*args, **kwargs)
        return wrapper
    return deco

@app.context_processor
def inject_can():
    return {
        "can": user_can,
        "signature": APP_SIGNATURE,
        "app_title": APP_TITLE,
    }

def ensure_custo_limpeza_in_protocolos():
    conn = get_conn()
    cur = conn.cursor()
    # PRAGMA só existe em SQLite, ignora em PostgreSQL
    try:
        cur.execute("PRAGMA table_info(protocolos)")
        cols = {r["name"] for r in cur.fetchall()}
        if "custo_limpeza" not in cols:
            cur.execute("ALTER TABLE protocolos ADD COLUMN custo_limpeza REAL DEFAULT 25")
            conn.commit()
    except Exception:
        pass
    conn.close()

ensure_custo_limpeza_in_protocolos()

def ensure_regiao_in_registos_limpeza():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(registos_limpeza)")
        cols = {r["name"] for r in cur.fetchall()}
        if "regiao" not in cols:
            cur.execute("ALTER TABLE registos_limpeza ADD COLUMN regiao TEXT")
            conn.commit()
    except Exception:
        pass
    conn.close()

ensure_regiao_in_registos_limpeza()
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------

# Esquema / seed
# -----------------------------------------------------------------------------
def ensure_schema_on_boot():
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    id_col = (
        "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        if is_postgres(conn)
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    
    # Tabelas principais
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS viaturas (
            id {id_col},
            matricula TEXT NOT NULL UNIQUE,
            descricao TEXT,
            filial TEXT,
            num_frota TEXT,
            ativo INTEGER DEFAULT 1,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS protocolos (
            id {id_col},
            nome TEXT NOT NULL UNIQUE,
            passos_json TEXT NOT NULL,
            frequencia_dias INTEGER,
            ativo INTEGER DEFAULT 1,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS registos_limpeza (
            id {id_col},
            viatura_id INTEGER NOT NULL,
            protocolo_id INTEGER NOT NULL,
            funcionario_id INTEGER NOT NULL,
            data_hora TEXT NOT NULL,
            estado TEXT DEFAULT 'concluido',
            observacoes TEXT,
            local TEXT,
            hora_inicio TEXT,
            hora_fim TEXT,
            extra_autorizada INTEGER DEFAULT 0,
            responsavel_autorizacao TEXT,
            verificacao_limpeza TEXT, 
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (viatura_id) REFERENCES viaturas(id) ON DELETE RESTRICT,
            FOREIGN KEY (protocolo_id) REFERENCES protocolos(id) ON DELETE RESTRICT,
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE RESTRICT
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS anexos (
            id {id_col},
            registo_id INTEGER NOT NULL,
            caminho TEXT NOT NULL,
            tipo TEXT,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (registo_id) REFERENCES registos_limpeza(id) ON DELETE CASCADE
        )
    """)
    # ...existing code...
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS funcionarios (
            id {id_col},
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            nome TEXT,
            role TEXT DEFAULT 'leitura',
            email TEXT,
            ativo INTEGER DEFAULT 1,
            regiao TEXT,
            descricao_viaturas TEXT,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
""")
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS pedidos_autorizacao (
        id {id_col},
        viatura_id INTEGER NOT NULL,
        funcionario_id INTEGER NOT NULL,
        data_pedido TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        validado INTEGER DEFAULT 0,
        validado_por INTEGER,
        data_validacao TEXT,
        FOREIGN KEY (viatura_id) REFERENCES viaturas(id),
        FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id),
        FOREIGN KEY (validado_por) REFERENCES funcionarios(id)
    )
""")
# ...existing code...
    # Perfis dinâmicos
    cur.execute(f"""CREATE TABLE IF NOT EXISTS roles (
        id {id_col},
        name TEXT NOT NULL UNIQUE
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS role_permissions (
        role_id INTEGER NOT NULL,
        perm TEXT NOT NULL,
        UNIQUE(role_id, perm),
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
    )""")

    # Índices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_funcionarios_username ON funcionarios(username)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_viaturas_matricula ON viaturas(matricula)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_viaturas_num_frota ON viaturas(num_frota)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_registos_data ON registos_limpeza(data_hora)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_registos_viatura ON registos_limpeza(viatura_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_registos_protocolo ON registos_limpeza(protocolo_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_registos_funcionario ON registos_limpeza(funcionario_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_registos_local ON registos_limpeza(local)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_anexos_registo ON anexos(registo_id)")
    
    # (Re)criar view detalhe
    cur.execute("DROP VIEW IF EXISTS vw_registos_detalhe")
    cur.execute("""
        CREATE VIEW vw_registos_detalhe AS
        SELECT
            r.id as registo_id,
            r.data_hora,
            r.hora_inicio,
            r.hora_fim,
            r.estado,
            r.observacoes,
            r.local,
            r.extra_autorizada,
            r.responsavel_autorizacao,
            v.matricula,
            v.num_frota,
            v.descricao as viatura_desc,
            v.filial,
            p.nome as protocolo,
            p.frequencia_dias,
            f.username as user,
            f.nome as funcionario
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
    """)

    # Seeds
    cur.execute("SELECT COUNT(*) FROM funcionarios WHERE username='admin'")
    if cur.fetchone()[0] == 0:
        cur.execute(
            f"INSERT INTO funcionarios (username,password,nome,role,ativo) VALUES ({ph},{ph},{ph},{ph},1)",
            ("admin", generate_password_hash("1234"), "Administrador", "admin")
        )
    cur.execute("SELECT 1 FROM funcionarios WHERE username='Pedro.fonte'")
    if not cur.fetchone():
        cur.execute(
            f"INSERT INTO funcionarios (username,password,nome,role,ativo) VALUES ({ph},{ph},{ph},{ph},1)",
            ("Pedro.fonte", generate_password_hash("1234"), "Pedro Fonte", "admin")
        )
    cur.execute("""
        UPDATE funcionarios
           SET role='leitura'
         WHERE role IS NULL OR TRIM(LOWER(role)) NOT IN ('admin','gestor','operador','leitura')
    """)

    cur.execute("SELECT COUNT(*) FROM viaturas")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            f"INSERT INTO viaturas (matricula, descricao, filial, num_frota, ativo) VALUES ({ph},{ph},{ph},{ph},1)",
            [
                ("AA-00-AA", "Autocarro Urbano", "Sede", "101"),
                ("BB-11-BB", "Autocarro Suburbano", "Filial Norte", "102"),
            ]
        )
    
    # Garantir coluna regiao em funcionarios
    try:
        cols = table_columns(conn, "funcionarios")
        if "email" not in cols:
            cur.execute("ALTER TABLE funcionarios ADD COLUMN email TEXT")
    except Exception:
        pass
        cols = set()
    if "regiao" not in cols:
        try: cur.execute("ALTER TABLE funcionarios ADD COLUMN regiao TEXT")
        except Exception: pass

    if "descricao_viaturas" not in cols:
        try: cur.execute("ALTER TABLE funcionarios ADD COLUMN descricao_viaturas TEXT")
        except Exception: pass

    # Garantir colunas extra em viaturas
    try:
        vcols = table_columns(conn, "viaturas")
        if "limpeza_validada" not in vcols:
            cur.execute("ALTER TABLE viaturas ADD COLUMN limpeza_validada INTEGER DEFAULT 0")
        for col in ("regiao","operacao","marca","modelo","tipo_protocolo"):
            if col not in vcols:
                try:
                    cur.execute(f"ALTER TABLE viaturas ADD COLUMN {col} TEXT")
                except Exception:
                    pass
        if "verificacao_limpeza" not in vcols:
            cur.execute("ALTER TABLE viaturas ADD COLUMN verificacao_limpeza TEXT DEFAULT NULL")  

    except Exception:
        pass

    

    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS alertas (
        id {id_col},
        viatura_id INTEGER NOT NULL,
        funcionario_origem_id INTEGER,
        destinatario_id INTEGER,
        data_hora TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        motivo TEXT NOT NULL,
        detalhes TEXT,
        lido INTEGER DEFAULT 0,
        FOREIGN KEY (viatura_id) REFERENCES viaturas(id) ON DELETE CASCADE,
        FOREIGN KEY (funcionario_origem_id) REFERENCES funcionarios(id) ON DELETE SET NULL,
        FOREIGN KEY (destinatario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
    )
""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alertas_dest ON alertas(destinatario_id, lido)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alertas_viat ON alertas(viatura_id, data_hora)")
    
    try:
        cols = table_columns(conn, "funcionarios")
    except Exception:
        cols = set()
    if "regiao" not in cols:
        try:
            cur.execute("ALTER TABLE funcionarios ADD COLUMN regiao TEXT")
        except Exception:
            pass    

    if "descricao_viaturas" not in cols:
        try:
            cur.execute("ALTER TABLE funcionarios ADD COLUMN descricao_viaturas TEXT")
        except Exception:
            pass

    cur.execute("SELECT COUNT(*) FROM protocolos")
    if cur.fetchone()[0] == 0:
        prot1 = {"passos": ["Inspeção interior", "Aspirar", "Desinfetar superfícies", "Vidros interiores", "Check final"]}
        prot2 = {"passos": ["Inspeção exterior", "Lavagem chassis", "Vidros exteriores", "Verificar níveis", "Check final"]}
        cur.execute(f"INSERT INTO protocolos (nome, passos_json, frequencia_dias, ativo) VALUES ({ph},{ph},{ph},1)",
                    ("Interior Standard", json.dumps(prot1, ensure_ascii=False), 7))
        cur.execute(f"INSERT INTO protocolos (nome, passos_json, frequencia_dias, ativo) VALUES ({ph},{ph},{ph},1)",
                    ("Exterior Standard", json.dumps(prot2, ensure_ascii=False), 14))
    else:
        cur.execute("UPDATE protocolos SET frequencia_dias=7  WHERE frequencia_dias IS NULL AND nome LIKE 'Interior%'")
        cur.execute("UPDATE protocolos SET frequencia_dias=14 WHERE frequencia_dias IS NULL AND nome LIKE 'Exterior%'")

    conn.commit()
    conn.close()

ensure_schema_on_boot()

# -----------------------------------------------------------------------------
def ensure_destinatario_id():
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "pedidos_autorizacao")
    if "destinatario_id" not in cols:
        cur.execute("ALTER TABLE pedidos_autorizacao ADD COLUMN destinatario_id INTEGER")
        conn.commit()
    conn.close()

ensure_destinatario_id()

def add_verificacao_limpeza_column():
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "registos_limpeza")
    if "verificacao_limpeza" not in cols:
        cur.execute("ALTER TABLE registos_limpeza ADD COLUMN verificacao_limpeza TEXT")
        conn.commit()
    conn.close()

add_verificacao_limpeza_column()

def ensure_num_frota_in_pedidos_autorizacao():
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "pedidos_autorizacao")
    if "num_frota" not in cols:
        cur.execute("ALTER TABLE pedidos_autorizacao ADD COLUMN num_frota TEXT")
        conn.commit()
    conn.close()

ensure_num_frota_in_pedidos_autorizacao()
def ensure_comentarios_verificacao_in_registos_limpeza():
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "registos_limpeza")
    if "comentarios_verificacao" not in cols:
        cur.execute("ALTER TABLE registos_limpeza ADD COLUMN comentarios_verificacao TEXT")
        conn.commit()
    conn.close()

ensure_comentarios_verificacao_in_registos_limpeza()

def ensure_verificacao_em_in_registos_limpeza():
    """
    Guarda a data/hora em que o gestor fez a inspeção (verificação),
    para conseguirmos calcular dias entre inspeções.
    """
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "registos_limpeza")
    if "verificacao_em" not in cols:
        cur.execute("ALTER TABLE registos_limpeza ADD COLUMN verificacao_em TEXT")
        conn.commit()
    conn.close()

ensure_verificacao_em_in_registos_limpeza()

def ensure_empresa_in_funcionarios():
    conn = get_conn()
    cur = conn.cursor()
    cols = table_columns(conn, "funcionarios")
    if "empresa" not in cols:
        cur.execute("ALTER TABLE funcionarios ADD COLUMN empresa TEXT")
        conn.commit()
    conn.close()

ensure_empresa_in_funcionarios()
# Autenticação
# -----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        conn = get_conn()
        cur = conn.cursor()
        ph = sql_placeholder(conn)
        # Compatível com SQLite e PostgreSQL
        if is_postgres(conn):
            cur.execute(f"SELECT * FROM funcionarios WHERE username = {ph} AND ativo=1", (username,))
        else:
            cur.execute(f"SELECT * FROM funcionarios WHERE username = {ph} COLLATE NOCASE AND ativo=1", (username,))
        user = cur.fetchone()
        conn.close()

        valid = False
        if user:
            dbpwd = user["password"]
            try:
                valid = check_password_hash(dbpwd, password)
            except Exception:
                valid = False
            if not valid and dbpwd == password:  # fallback legado
                valid = True

        if valid:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = normalize_role(user["role"])

            flash(f"Bem vindo, {user['nome'] or user['username']}!", "info")

            def _first_allowed(role):
                if has_perm(role, "dashboard:view"):   return "home"
                if has_perm(role, "registos:view"):    return "registos"
                if has_perm(role, "viaturas:view"):    return "viaturas"
                if has_perm(role, "protocolos:view"):  return "protocolos"
                return "sem_permissao"

            return redirect(url_for(_first_allowed(session["role"])))

        flash("Credenciais inválidas.", "danger")

    return render_template("login.html", signature=APP_SIGNATURE)

@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão terminada.", "info")
    return redirect(url_for("login"))

@app.route("/sem-permissao")
def sem_permissao():
    return render_template("403.html", signature=APP_SIGNATURE), 403

# -----------------------------------------------------------------------------
# Dashboard (/)
# -----------------------------------------------------------------------------

def get_counts(col):
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    cur.execute(f"SELECT {col} AS k, COUNT(*) AS n FROM viaturas WHERE {col} IS NOT NULL AND TRIM({col})<>'' GROUP BY {col} ORDER BY n DESC, k")
    rows = cur.fetchall()
    conn.close()
    return [r["k"] for r in rows], [r["n"] for r in rows]

@app.route("/")
@login_required
@require_perm("dashboard:view")
def home():
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    mes = request.args.get("mes")
    user_role = session.get("role")
    user_id = session.get("user_id")
    regiao_gestor = None

    # Se for gestor, obter a sua região
    if user_role == "gestor":
        cur.execute(f"SELECT regiao FROM funcionarios WHERE id={ph}", (user_id,))
        row = cur.fetchone()
        regiao_gestor = (row["regiao"] or "").strip() if row else None

    reg_labels, reg_values = get_counts("regiao")
    op_labels, op_values   = get_counts("operacao")
    mar_labels, mar_values = get_counts("marca")
    mod_labels, mod_values = get_counts("modelo")
    tip_labels, tip_values = get_counts("tipo_protocolo")
    
    # Helper para adicionar filtro de região e mês
    def filtro_mes_regiao(sql, params, alias_registos="r", alias_viaturas="v"):
        if mes:
            if is_postgres(conn):
                sql += f" AND TO_CHAR({alias_registos}.data_hora, 'YYYY-MM') = {ph}"
            else:
                sql += f" AND strftime('%Y-%m', {alias_registos}.data_hora) = {ph}"
            params.append(mes)
        if regiao_gestor:
            sql += f" AND {alias_viaturas}.regiao = {ph}"
            params.append(regiao_gestor)
        return sql, params

    # Protocolos
    cur.execute("SELECT id, nome, COALESCE(frequencia_dias, 0) AS frequencia_dias FROM protocolos WHERE ativo=1 ORDER BY nome")
    protocolos = [dict(r) for r in cur.fetchall()]

    # Viaturas (filtradas por região se gestor/operador)
    viaturas_sql = "SELECT id, matricula, descricao, filial, num_frota, regiao FROM viaturas WHERE ativo=1"
    viaturas_params = []
    regiao_user = None
    desc_list_user: list[str] = []
    if user_role in ("gestor", "operador"):
        cur.execute(f"SELECT regiao, descricao_viaturas FROM funcionarios WHERE id={ph}", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        if regiao_user:
            viaturas_sql += f" AND regiao = {ph}"
            viaturas_params.append(regiao_user)
        elif user_role == "operador":
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            desc_list_user = parse_descricao_viaturas(descricao_user)
            if desc_list_user:
                placeholders = ",".join([ph] * len(desc_list_user))
                viaturas_sql += f" AND COALESCE(descricao,'') IN ({placeholders})"
                viaturas_params.extend(desc_list_user)
    viaturas_sql += " ORDER BY filial, matricula"
    cur.execute(viaturas_sql, viaturas_params)
    viaturas = [dict(r) for r in cur.fetchall()]

    # Datas de referência em Portugal continental (parâmetros, não relógio do servidor)
    today_str = today_pt_iso()
    week_start_str = (today_pt() - timedelta(days=6)).isoformat()
    month_str = today_pt().strftime("%Y-%m")
    if is_postgres(conn):
        dt_fmt = "TO_CHAR(r.data_hora, 'YYYY-MM')"
        dt_today_sql = f"date(r.data_hora) = {ph}"
        dt_today_param = today_str
        dt_7days_sql = f"date(r.data_hora) >= {ph}"
        dt_7days_param = week_start_str
        dt_eq_sql = f"{dt_fmt} = {ph}"
        dt_eq_param = month_str
    else:
        dt_fmt = "strftime('%Y-%m', r.data_hora)"
        dt_today_sql = f"date(r.data_hora) = {ph}"
        dt_today_param = today_str
        dt_7days_sql = f"date(r.data_hora) >= {ph}"
        dt_7days_param = week_start_str
        dt_eq_sql = f"{dt_fmt} = {ph}"
        dt_eq_param = month_str

    # Última limpeza por viatura/protocolo (filtrada por região)
    last_map_sql = """
        SELECT r.viatura_id, r.protocolo_id, MAX(datetime(r.data_hora)) AS ult
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE 1=1
    """
    last_map_params = []
    if regiao_gestor:
        last_map_sql += f" AND v.regiao = {ph}"
        last_map_params.append(regiao_gestor)
    last_map_sql += " GROUP BY r.viatura_id, r.protocolo_id"
    cur.execute(last_map_sql, last_map_params)
    last_map = {(r["viatura_id"], r["protocolo_id"]): r["ult"] for r in cur.fetchall()}

    # Última (qualquer) por viatura (filtrada por região)
    last_any_sql = """
        SELECT v.id as viatura_id, MAX(datetime(r.data_hora)) AS ult
        FROM viaturas v
        LEFT JOIN registos_limpeza r ON v.id = r.viatura_id
        WHERE v.ativo=1
    """
    last_any_params = []
    if user_role in ("gestor", "operador") and regiao_user:
        last_any_sql += f" AND v.regiao = {ph}"
        last_any_params.append(regiao_user)
    last_any_sql += " GROUP BY v.id"
    cur.execute(last_any_sql, last_any_params)
    last_any = {r["viatura_id"]: r["ult"] for r in cur.fetchall()}

    # Limpezas hoje (por região se gestor)
    limpezas_hoje_sql = f"""
        SELECT r.viatura_id, COUNT(*) as n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_today_sql}
    """
    limpezas_hoje_params = [dt_today_param]
    if regiao_gestor:
        limpezas_hoje_sql += f" AND v.regiao = {ph}"
        limpezas_hoje_params.append(regiao_gestor)
    limpezas_hoje_sql += " GROUP BY r.viatura_id"
    cur.execute(limpezas_hoje_sql, limpezas_hoje_params)
    limpezas_hoje = {r["viatura_id"]: r["n"] for r in cur.fetchall()}
    for v in viaturas:
        v["limpeza_repetida"] = limpezas_hoje.get(v["id"], 0) > 1

    # Limpezas hoje (por região se gestor/operador)
    limpas_hoje_sql = f"""
        SELECT r.viatura_id, COUNT(*) as n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_today_sql}
    """
    limpas_hoje_params = [dt_today_param]
    if user_role in ("gestor", "operador"):
        if regiao_user:
            limpas_hoje_sql += f" AND v.regiao = {ph}"
            limpas_hoje_params.append(regiao_user)
    limpas_hoje_sql += " GROUP BY r.viatura_id"
    cur.execute(limpas_hoje_sql, limpas_hoje_params)
    limpas_hoje_map = {r["viatura_id"]: r["n"] for r in cur.fetchall()}

    for v in viaturas:
        v["limpa_hoje"] = v["id"] in limpas_hoje_map
        v["limpeza_repetida"] = limpas_hoje_map.get(v["id"], 0) > 1

    # KPI: registos hoje (total de registos de limpeza criados hoje)
    kpi_today_sql = f"""
        SELECT COUNT(*) AS n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_today_sql}
    """
    kpi_today_params = [dt_today_param]
    if user_role in ("gestor", "operador"):
        if regiao_user:
            kpi_today_sql += f" AND v.regiao = {ph}"
            kpi_today_params.append(regiao_user)
    cur.execute(kpi_today_sql, kpi_today_params)
    kpi_today = cur.fetchone()["n"]

    # KPI: viaturas limpas hoje (viaturas distintas limpas pelo menos uma vez hoje)
    kpi_today_veh_sql = f"""
        SELECT COUNT(DISTINCT r.viatura_id) AS n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_today_sql}
    """
    kpi_today_veh_params = [dt_today_param]
    if user_role in ("gestor", "operador"):
        if regiao_user:
            kpi_today_veh_sql += f" AND v.regiao = {ph}"
            kpi_today_veh_params.append(regiao_user)
    cur.execute(kpi_today_veh_sql, kpi_today_veh_params)
    kpi_today_veh = cur.fetchone()["n"]

    # KPI: total de limpezas hoje (inclui extra)
    kpi_total_limpezas_sql = f"""
        SELECT COUNT(*) AS n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_today_sql}
    """
    kpi_total_limpezas_params = [dt_today_param]
    if user_role in ("gestor", "operador"):
        if regiao_user:
            kpi_total_limpezas_sql += f" AND v.regiao = {ph}"
            kpi_total_limpezas_params.append(regiao_user)
    cur.execute(kpi_total_limpezas_sql, kpi_total_limpezas_params)
    kpi_total_limpezas = cur.fetchone()["n"]

    # KPI: registos últimos 7 dias
    kpi_week_sql = f"""
        SELECT COUNT(*) AS n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_7days_sql}
    """
    kpi_week_params = [dt_7days_param]
    if regiao_gestor:
        kpi_week_sql += f" AND v.regiao = {ph}"
        kpi_week_params.append(regiao_gestor)
    cur.execute(kpi_week_sql, kpi_week_params)
    kpi_week = cur.fetchone()["n"]

    # KPI: registos este mês
    kpi_month_sql = f"""
        SELECT COUNT(*) AS n
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE {dt_eq_sql}
    """
    kpi_month_params = [dt_eq_param]
    if regiao_gestor:
        kpi_month_sql += f" AND v.regiao = {ph}"
        kpi_month_params.append(regiao_gestor)
    cur.execute(kpi_month_sql, kpi_month_params)
    kpi_month = cur.fetchone()["n"]

    # Limpezas por local
    sql_local = """
        SELECT COALESCE(r.local,'(Sem local)') as label, COUNT(*) as qty
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE 1=1
    """
    params_local = []
    sql_local, params_local = filtro_mes_regiao(sql_local, params_local)
    sql_local += " GROUP BY COALESCE(r.local,'(Sem local)') ORDER BY qty DESC, label"
    cur.execute(sql_local, params_local)
    chart_local = [(r["label"], r["qty"]) for r in cur.fetchall()]

    # Limpezas por funcionário
    sql_func = """
        SELECT f.username as label, COUNT(*) as qty
        FROM registos_limpeza r
        JOIN funcionarios f ON f.id = r.funcionario_id
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE 1=1
    """
    params_func = []
    sql_func, params_func = filtro_mes_regiao(sql_func, params_func)
    sql_func += " GROUP BY r.funcionario_id ORDER BY qty DESC, label"
    cur.execute(sql_func, params_func)
    chart_func = [(r["label"], r["qty"]) for r in cur.fetchall()]

    # Viaturas distintas por protocolo
    sql_proto = """
        SELECT p.nome as label, COUNT(DISTINCT r.viatura_id) as qty
        FROM registos_limpeza r
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN viaturas v ON v.id = r.viatura_id
        WHERE 1=1
    """
    params_proto = []
    sql_proto, params_proto = filtro_mes_regiao(sql_proto, params_proto)
    sql_proto += " GROUP BY r.protocolo_id ORDER BY p.nome"
    cur.execute(sql_proto, params_proto)
    chart_proto = [(r["label"], r["qty"]) for r in cur.fetchall()]

    # ...restante código igual...
    # (continua igual ao teu original a partir daqui)

    # Média de dias desde última limpeza
    hoje = today_pt()
    dias_por_viatura = []
    for v in viaturas:
        iso = last_any.get(v["id"])
        if not iso:
           continue
        dt = datetime.fromisoformat(iso).date()
        dias_por_viatura.append((hoje - dt).days)
    media_dias_ultima = round(sum(dias_por_viatura)/len(dias_por_viatura), 2) if dias_por_viatura else 0.0
    total_viaturas = len(viaturas)

    # Limpezas por duração (mantém original)
    cur.execute("""
        SELECT r.protocolo_id, p.nome AS nome, r.data_hora, r.hora_inicio, r.hora_fim
        FROM registos_limpeza r
        JOIN protocolos p ON p.id = r.protocolo_id
        WHERE r.hora_inicio IS NOT NULL AND r.hora_fim IS NOT NULL
    """)
    from collections import defaultdict
    sum_min, cnt_min = defaultdict(int), defaultdict(int)
    for r in cur.fetchall():
        try:
            d = datetime.fromisoformat(r["data_hora"]).date()
            h1 = datetime.fromisoformat(f"{d} {r['hora_inicio']}:00")
            h2 = datetime.fromisoformat(f"{d} {r['hora_fim']}:00")
            mins = max(0, int((h2 - h1).total_seconds()//60))
            sum_min[r["nome"]] += mins; cnt_min[r["nome"]] += 1
        except Exception:
            pass
    chart_dur = [(nome, round(sum_min[nome]/cnt_min[nome],1)) for nome in sorted(sum_min.keys())]

    # Top 10 atraso
    rows_atraso = []
    rows_top10 = []
    all_last_any_days = []
    for v in viaturas:
        iso_any = last_any.get(v["id"])
        if not iso_any: continue
        dta = datetime.fromisoformat(iso_any).date()
        all_last_any_days.append({
            "num_frota": v.get("num_frota") or "",
            "matricula": v["matricula"],
            "filial": v.get("filial") or "",
            "dias_sem_limpeza": (hoje - dta).days,
            "ultima_qualquer": datetime.fromisoformat(iso_any).isoformat(sep=" "),
        })
    rows_top10 = sorted(all_last_any_days, key=lambda r: (r["dias_sem_limpeza"], r["matricula"]))[:10]

    for v in viaturas:
        vinfo = {
            "num_frota": v.get("num_frota") or "",
            "matricula": v["matricula"],
            "filial": v.get("filial") or "",
            "ultima_qualquer": None,
            "dias_sem_limpeza": None,
            "por_protocolo": {},
            "delta_protocolos": None,
            "tem_atraso": False,
            "atraso_por_dias": 0,
            "limpa_hoje": v["id"] in limpas_hoje_map,
        }
        last_dates, max_over, algum_atraso = [], 0, False

        for p in protocolos:
            iso = last_map.get((v["id"], p["id"]))
            last_dt = datetime.fromisoformat(iso) if iso else None
            dias = (hoje - last_dt.date()).days if last_dt else None
            freq = int(p["frequencia_dias"]) if p["frequencia_dias"] else None
            atraso, overdue_by = False, 0

            if last_dt:
                last_dates.append(last_dt)
                if freq and dias is not None and dias > freq:
                    atraso, overdue_by = True, dias - freq
            else:
                if freq:
                    atraso, overdue_by = True, 10_000

            if atraso:
                algum_atraso = True
                max_over = max(max_over, overdue_by)

            vinfo["por_protocolo"][p["id"]] = {
                "nome": p["nome"],
                "ultima": last_dt.isoformat(sep=" ") if last_dt else None,
                "dias": dias,
                "freq": freq,
                "atraso": atraso,
            }

        if last_dates:
            ultima = max(last_dates)
            vinfo["ultima_qualquer"] = ultima.isoformat(sep=" ")
            vinfo["dias_sem_limpeza"] = (hoje - ultima.date()).days
            if len(last_dates) >= 2:
                vinfo["delta_protocolos"] = abs((max(last_dates).date()) - (min(last_dates).date())).days

        vinfo["tem_atraso"] = algum_atraso
        vinfo["atraso_por_dias"] = max_over
        if vinfo["tem_atraso"]:
            rows_atraso.append(vinfo)

    rows_atraso.sort(key=lambda r: (-r["atraso_por_dias"], r["matricula"]))

    charts = {
        "kpi_today": kpi_today,
        "kpi_week": kpi_week,
        "kpi_month": kpi_month,
        "kpi_today_veh": kpi_today_veh,
        "kpi_total_limpezas": kpi_total_limpezas,
        "proto_labels": [l for (l, _) in chart_proto],
        "proto_values": [v for (_, v) in chart_proto],
        "avg_days": media_dias_ultima,
        "fleet_size": total_viaturas,
        "local_labels": [l for (l, _) in chart_local],
        "local_values": [v for (_, v) in chart_local],
        "func_labels": [l for (l, _) in chart_func],
        "func_values": [v for (_, v) in chart_func],
        "dur_labels": [l for (l, _) in chart_dur],
        "dur_values": [v for (_, v) in chart_dur],
        "reg_labels": reg_labels, "reg_values": reg_values,
        "op_labels":  op_labels,  "op_values":  op_values,
        "mar_labels": mar_labels, "mar_values": mar_values,
        "mod_labels": mod_labels, "mod_values": mod_values,
        "tip_labels": tip_labels, "tip_values": tip_values,
    }

    # Viaturas limpas por protocolo (hoje, filtradas por região se gestor)
    viaturas_proto_sql = f"""
        SELECT r.protocolo_id, p.nome as protocolo_nome, v.matricula, v.num_frota, v.descricao
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        WHERE {dt_today_sql}
    """
    viaturas_proto_params = [dt_today_param]
    if regiao_gestor:
        viaturas_proto_sql += " AND v.regiao = ?"
        viaturas_proto_params.append(regiao_gestor)
    viaturas_proto_sql += " ORDER BY p.nome, v.matricula"
    cur.execute(viaturas_proto_sql, viaturas_proto_params)
    viaturas_por_protocolo = {}
    for row in cur.fetchall():
        pid = row["protocolo_id"]
        nome = row["protocolo_nome"]
        if pid not in viaturas_por_protocolo:
            viaturas_por_protocolo[pid] = {"nome": nome, "viaturas": []}
        viaturas_por_protocolo[pid]["viaturas"].append({
            "matricula": row["matricula"],
            "num_frota": row["num_frota"] or "",
            "descricao": row["descricao"] or ""
        })

    # Pedidos de autorização pendentes (para gestor/admin)
    pedidos_pendentes = []
    if user_role in ["admin", "gestor"]:
        gestor_id = user_id
        ped_hoje_sql, ped_hoje_val = sql_date_eq_today("pa.data_pedido", conn)
        cur.execute(f"""
            SELECT pa.id, v.matricula, v.num_frota, f.nome as operador
            FROM pedidos_autorizacao pa
            JOIN viaturas v ON v.id = pa.viatura_id
            JOIN funcionarios f ON f.id = pa.funcionario_id
            WHERE pa.validado=0 AND pa.destinatario_id={ph} AND {ped_hoje_sql}
            ORDER BY pa.data_pedido DESC
        """, (gestor_id, ped_hoje_val))
        pedidos_pendentes = [dict(r) for r in cur.fetchall()]

    conn.close()

    for v in viaturas:
        if "limpeza_validada" not in v:
            v["limpeza_validada"] = 0

    return render_template("home.html",
        charts=charts,
        protocolos=protocolos,
        rows=rows_atraso,
        top10=rows_top10,
        signature=APP_SIGNATURE,
        viaturas=viaturas,
        viaturas_por_protocolo=viaturas_por_protocolo,
        pedidos_pendentes=pedidos_pendentes,
        mes=mes
    )
@app.route("/pedidos_autorizacao")
@login_required
@require_perm("dashboard:view")
def pedidos_autorizacao():
    gestor_id = session.get("user_id")
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    ped_hoje_sql, ped_hoje_val = sql_date_eq_today("pa.data_pedido", conn)
    cur.execute(f"""
        SELECT pa.id, v.matricula, v.num_frota, f.nome as operador
        FROM pedidos_autorizacao pa
        JOIN viaturas v ON v.id = pa.viatura_id
        JOIN funcionarios f ON f.id = pa.funcionario_id
        WHERE pa.validado=0 AND pa.destinatario_id={ph} AND {ped_hoje_sql}
        ORDER BY pa.data_pedido DESC
    """, (gestor_id, ped_hoje_val))
    pedidos = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("pedidos_autorizacao.html", pedidos=pedidos, signature=APP_SIGNATURE)

@app.route("/validar_pedido_autorizacao/<int:pedido_id>", methods=["POST"])
@login_required
@require_perm("dashboard:view")
def validar_pedido_autorizacao(pedido_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    # Compatível com SQLite e PostgreSQL para timestamp
    if is_postgres(conn):
        cur.execute(f"UPDATE pedidos_autorizacao SET validado=1, validado_por={ph}, data_validacao=NOW() WHERE id={ph}", (session["user_id"], pedido_id))
    else:
        cur.execute(f"UPDATE pedidos_autorizacao SET validado=1, validado_por={ph}, data_validacao=CURRENT_TIMESTAMP WHERE id={ph}", (session["user_id"], pedido_id))
    conn.commit()
    conn.close()
    flash("Pedido autorizado!", "success")
    return redirect(url_for("pedidos_autorizacao"))

# -----------------------------------------------------------------------------
# Viaturas
# -----------------------------------------------------------------------------
@app.route("/viaturas/exportar")
@login_required
@require_perm("viaturas:view")
def exportar_viaturas_csv():
    q_matricula = (request.args.get("matricula") or "").strip()
    q_num_frota = (request.args.get("num_frota") or "").strip()
    f_regiao = (request.args.get("regiao") or "").strip()
    access_desc_list: list[str] = []
    f_operacao = (request.args.get("operacao") or "").strip()
    f_marca = (request.args.get("marca") or "").strip()
    f_modelo = (request.args.get("modelo") or "").strip()
    f_ativo = (request.args.get("ativo") or "").strip()

    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    where = ["1=1"]
    params = []

    # Restrições de acesso pelo perfil
    if session.get("role") in ("gestor", "operador"):
        cur.execute(
            f"SELECT regiao, descricao_viaturas FROM funcionarios WHERE id={ph}",
            (session.get("user_id"),),
        )
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row else ""
        if regiao_user:
            f_regiao = regiao_user
        elif session.get("role") == "operador":
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            access_desc_list = parse_descricao_viaturas(descricao_user)
    if q_matricula:
        where.append(f"v.matricula LIKE {ph}")
        params.append(f"%{q_matricula}%")
    if q_num_frota:
        where.append(f"(v.numero_frota = {ph} OR v.num_frota = {ph})")
        params.extend([q_num_frota, q_num_frota])
    if f_regiao:
        where.append(f"COALESCE(v.regiao,'') = {ph}")
        params.append(f_regiao)
    if access_desc_list:
        placeholders = ",".join([ph] * len(access_desc_list))
        where.append(f"COALESCE(v.descricao,'') IN ({placeholders})")
        params.extend(access_desc_list)
    if f_operacao:
        where.append(f"COALESCE(v.operacao,'') = {ph}")
        params.append(f_operacao)
    if f_marca:
        where.append(f"COALESCE(v.marca,'') = {ph}")
        params.append(f_marca)
    if f_modelo:
        where.append(f"COALESCE(v.modelo,'') = {ph}")
        params.append(f_modelo)
    if f_ativo in ("0", "1"):
        where.append(f"v.ativo = {ph}")
        params.append(int(f_ativo))

    cur.execute(f"""
    SELECT v.id, v.matricula,
           COALESCE(v.numero_frota, v.num_frota) AS numero_frota,
           v.regiao, v.operacao, v.marca, v.modelo, v.tipo_protocolo,
           v.descricao, v.filial, v.num_frota, v.ativo, v.criado_em
    FROM viaturas v
    WHERE { " AND ".join(where) }
    ORDER BY v.matricula
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    import io, csv as _csv
    sio = io.StringIO()
    headers = ["matricula", "nº de frota", "Região", "Operação", "Marca", "Modelo", "Tipo de Protocolo", "Ativo",
               "descricao", "filial", "num_frota", "criado_em", "id"]
    w = _csv.writer(sio, delimiter=';')
    w.writerow(headers)
    for r in rows:
        w.writerow([
            r["matricula"], r["numero_frota"] or "", r["regiao"] or "", r["operacao"] or "",
            r["marca"] or "", r["modelo"] or "", r["tipo_protocolo"] or "",
            "Sim" if int(r["ativo"] or 0) else "Não", r["descricao"] or "", r["filial"] or "",
            r["num_frota"] or "", r["criado_em"] or "", r["id"],
        ])
    data = sio.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name="viaturas_export.csv")


@app.route("/viaturas", methods=["GET", "POST"])
@login_required
@require_perm("viaturas:view")
def viaturas():
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    viaturas_cols = table_columns(conn, "viaturas")
    has_numero_frota = "numero_frota" in viaturas_cols
    num_frota_expr = "COALESCE(v.numero_frota, v.num_frota)" if has_numero_frota else "v.num_frota"

    # filtros
    q_matricula = (request.args.get("matricula") or "").strip()
    q_num_frota = (request.args.get("num_frota") or "").strip()
    f_regiao = (request.args.get("regiao") or "").strip()
    f_operacao = (request.args.get("operacao") or "").strip()
    f_marca = (request.args.get("marca") or "").strip()
    f_modelo = (request.args.get("modelo") or "").strip()
    f_tipo = (request.args.get("tipo_protocolo") or "").strip()
    f_ativo = (request.args.get("ativo") or "").strip()
    f_filial = (request.args.get("filial") or "").strip()
    f_desc_list: list[str] = []

    # Se for gestor, força filtro pela sua região
    if session.get("role") in ("gestor", "operador"):
        cur.execute(
            f"SELECT regiao, descricao_viaturas FROM funcionarios WHERE id={ph}",
            (session.get("user_id"),),
        )
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row else ""
        if regiao_user:
            f_regiao = regiao_user
        else:
            # Operador com região vazia: restringir por lista de descrições
            if session.get("role") == "operador":
                descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
                f_desc_list = parse_descricao_viaturas(descricao_user)

    if request.method == "POST":
        matricula = (request.form.get("matricula") or "").strip()
        num_frota = (request.form.get("num_frota") or "").strip()
        regiao = (request.form.get("regiao") or "").strip()
        operacao = (request.form.get("operacao") or "").strip()
        marca = (request.form.get("marca") or "").strip()
        modelo = (request.form.get("modelo") or "").strip()
        tipo_protocolo = (request.form.get("tipo_protocolo") or "").strip()
        descricao = (request.form.get("descricao") or "").strip()
        filial = (request.form.get("filial") or "").strip()
        ativo = 1

        if not matricula:
            flash("A matrícula é obrigatória.", "danger")
        else:
            # Verifica se já existe viatura com esta matrícula
            cur.execute(f"SELECT id FROM viaturas WHERE matricula = {ph}", (matricula,))
            existe = cur.fetchone()
            if existe:
                flash("Já existe uma viatura com essa matrícula. Verifique a lista existente.", "danger")
            else:
                try:
                    cur.execute(f"""
                        INSERT INTO viaturas (matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                    """, (matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo))
                    conn.commit()
                    flash("Viatura inserida com sucesso.", "success")
                except Exception as e:
                    flash(f"Erro ao inserir viatura: {e}", "danger")
                
    if not f_ativo:
        f_ativo = "1"

    where = ["1=1"]
    params = []
    if q_matricula:
        where.append(f"v.matricula LIKE {ph}"); params.append(f"%{q_matricula}%")
    if q_num_frota:
        if has_numero_frota:
            where.append(f"(v.numero_frota = {ph} OR v.num_frota = {ph})")
            params.extend([q_num_frota, q_num_frota])
        else:
            where.append(f"v.num_frota = {ph}")
            params.append(q_num_frota)
    if f_regiao:
        where.append(f"COALESCE(v.regiao,'') = {ph}"); params.append(f_regiao)
    if f_desc_list:
        placeholders = ",".join([ph] * len(f_desc_list))
        where.append(f"COALESCE(v.descricao,'') IN ({placeholders})")
        params.extend(f_desc_list)
    if f_operacao:
        where.append(f"COALESCE(v.operacao,'') = {ph}"); params.append(f_operacao)
    if f_marca:
        where.append(f"COALESCE(v.marca,'') = {ph}"); params.append(f_marca)
    if f_modelo:
        where.append(f"COALESCE(v.modelo,'') = {ph}"); params.append(f_modelo)
    if f_tipo:
        where.append(f"COALESCE(v.tipo_protocolo,'') = {ph}"); params.append(f_tipo)
    if f_ativo in ("0", "1"):
        where.append(f"v.ativo = {ph}"); params.append(int(f_ativo))
    if f_filial:
        where.append(f"COALESCE(v.filial,'') = {ph}"); params.append(f_filial)

    cur.execute(f"""
        WITH last AS (
          SELECT r.*
          FROM registos_limpeza r
          JOIN (
            SELECT viatura_id, MAX(datetime(data_hora)) AS ult
            FROM registos_limpeza GROUP BY viatura_id
          ) m ON m.viatura_id=r.viatura_id AND datetime(r.data_hora)=m.ult
        ),
        verificados AS (
          SELECT viatura_id, COUNT(*) AS n
          FROM registos_limpeza
          WHERE verificacao_limpeza IS NOT NULL AND TRIM(verificacao_limpeza) <> ''
          GROUP BY viatura_id
        )
        SELECT v.id, v.matricula, v.descricao, v.filial,
               {num_frota_expr} AS num_frota,
               v.regiao, v.operacao, v.marca, v.modelo, v.tipo_protocolo, v.ativo,
               l.local AS ultima_local, l.hora_inicio, l.hora_fim,
               f.username AS ultima_user
        FROM viaturas v
        LEFT JOIN last l ON l.viatura_id = v.id
        LEFT JOIN funcionarios f ON f.id = l.funcionario_id
        LEFT JOIN verificados ver ON ver.viatura_id = v.id
        WHERE { " AND ".join(where) }
        ORDER BY v.filial, v.matricula
    """, params)
    vs = [dict(row) for row in cur.fetchall()]
    
    cur.execute("SELECT id, nome, frequencia_dias FROM protocolos WHERE UPPER(nome) IN ('PROTOCOLO B', 'PROTOCOLO C')")
    protocolos_bc = {r["nome"].upper(): dict(r) for r in cur.fetchall()}

    hoje = today_pt()
    for v in vs:
        v["tem_atraso"] = False
        for nome in ("PROTOCOLO B", "PROTOCOLO C"):
            ins_key = "b" if nome.endswith("B") else "c"
            prot = protocolos_bc.get(nome)
            if not prot:
                v[f"dias_{nome.replace(' ', '_').lower()}"] = None
                v[f"freq_{nome.replace(' ', '_').lower()}"] = None
                v[f"dias_inspecao_{ins_key}"] = None
                v[f"freq_inspecao_{ins_key}"] = None
                continue
            cur.execute(f"""
                SELECT MAX(date(r.data_hora)) as ult
                FROM registos_limpeza r
                WHERE r.viatura_id={ph} AND r.protocolo_id={ph}
            """, (v["id"], prot["id"]))
            ult = cur.fetchone()["ult"]
            if ult:
                dias = (hoje - datetime.fromisoformat(ult).date()).days
            else:
                dias = None
            v[f"dias_{nome.replace(' ', '_').lower()}"] = dias
            v[f"freq_{nome.replace(' ', '_').lower()}"] = prot["frequencia_dias"]

            # Dias desde a última inspeção (verificação registada pelo gestor)
            cur.execute(f"""
                SELECT MAX(date(COALESCE(r.verificacao_em, r.data_hora))) as ult
                FROM registos_limpeza r
                WHERE r.viatura_id={ph}
                  AND r.protocolo_id={ph}
                  AND r.verificacao_limpeza IS NOT NULL
                  AND TRIM(r.verificacao_limpeza) <> ''
            """, (v["id"], prot["id"]))
            ult_i = cur.fetchone()["ult"]
            if ult_i:
                dias_inspecao = (hoje - datetime.fromisoformat(ult_i).date()).days
            else:
                dias_inspecao = None
            v[f"dias_inspecao_{ins_key}"] = dias_inspecao
            v[f"freq_inspecao_{ins_key}"] = prot["frequencia_dias"]

            # Verifica atraso
            if dias is not None and prot["frequencia_dias"] is not None and dias > prot["frequencia_dias"]:
                v["tem_atraso"] = True

    # Ordenar: primeiro as viaturas com atraso, depois as restantes
    vs = sorted(vs, key=lambda v: (not v["tem_atraso"], v["matricula"]))

    def _opts(col):
        cur.execute(f"SELECT DISTINCT {col} AS v FROM viaturas WHERE {col} IS NOT NULL AND TRIM({col})<>'' ORDER BY 1")
        return [r["v"] for r in cur.fetchall()]

    filtros = {
        "regiao": _opts("regiao"),
        "operacao": _opts("operacao"),
        "marca": _opts("marca"),
        "modelo": _opts("modelo"),
        "tipo_protocolo": _opts("tipo_protocolo"),
        "filial": _opts("filial"),
    }

    conn.close()
    
    return render_template("viaturas.html", viaturas=vs, filtros=filtros, signature=APP_SIGNATURE)
    
@app.route("/registos/<int:registo_id>/verificar", methods=["GET", "POST"])
@login_required
@require_perm("dashboard:view")
def verificar_limpeza(registo_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    if request.method == "POST":
        verificacao = (request.form.get("verificacao_limpeza") or "").strip()
        comentarios = (request.form.get("comentarios_verificacao") or "").strip()
        if not verificacao:
            flash("Selecione o tipo de verificação.", "danger")
            conn.close()
            return redirect(url_for("verificar_limpeza", registo_id=registo_id))

        if verificacao.lower() in {"não conforme", "nao conforme"} and not comentarios:
            flash("Indique o comentário quando a verificação é 'não conforme'.", "danger")
            conn.close()
            return redirect(url_for("verificar_limpeza", registo_id=registo_id))

        comentarios_to_save = comentarios if verificacao.lower() in {"não conforme", "nao conforme"} else None
        cur.execute(
            f"""UPDATE registos_limpeza
                SET verificacao_limpeza={ph},
                    comentarios_verificacao={ph},
                    verificacao_em={ph}
                WHERE id={ph}""",
            (verificacao, comentarios_to_save, now_pt_iso(), registo_id),
        )
        conn.commit()
        conn.close()
        flash("Verificação de limpeza registada.", "success")
        return redirect(url_for("registos"))
    cur.execute(f"SELECT * FROM registos_limpeza WHERE id={ph}", (registo_id,))
    registo = cur.fetchone()
    conn.close()
    if not registo:
        flash("Registo não encontrado.", "danger")
        return redirect(url_for("registos"))
    return render_template("verificar_limpeza.html", registo=registo, signature=APP_SIGNATURE)

@app.route("/gestor/verificacoes", methods=["GET", "POST"])
@login_required
@require_perm("dashboard:view")
def gestor_verificacoes():
    # A funcionalidade pedida é exclusiva ao perfil gestor.
    if session.get("role") != "gestor":
        flash("Apenas o perfil gestor pode registar verificações em lote.", "danger")
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)

    # Região do gestor
    cur.execute("SELECT regiao FROM funcionarios WHERE id=?", (session.get("user_id"),))
    row = cur.fetchone()
    regiao_gestor = (row["regiao"] or "").strip() if row else None

    if request.method == "POST":
        selected = request.form.getlist("registos")
        if not selected:
            flash("Selecione pelo menos um registo para inspecionar ou alterar.", "warning")
            conn.close()
            return redirect(url_for("gestor_verificacoes"))

        erros = _processar_verificacoes_gestor(cur, ph, selected)
        if erros:
            conn.rollback()
            conn.close()
            flash(" | ".join(erros), "danger")
            return redirect(url_for("gestor_verificacoes"))

        conn.commit()
        conn.close()
        flash("Verificações registadas/atualizadas com sucesso.", "success")
        return redirect(url_for("gestor_verificacoes"))

    pendentes = _gestor_ultimos_registos_verificacao(cur, ph, regiao_gestor, apenas_pendentes=True)
    verificados = _gestor_ultimos_registos_verificacao(cur, ph, regiao_gestor, apenas_pendentes=False)
    conn.close()

    return render_template(
        "gestor_verificacoes.html",
        pendentes=pendentes,
        verificados=verificados,
        signature=APP_SIGNATURE,
    )

@app.route("/registos/<int:rid>", methods=["GET", "POST"])
@login_required
@require_perm("registos:view")
def registo_detalhe(rid):
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    if request.method == "POST" and user_can("dashboard:view"):
        # Observações e anexos
        nova_obs = (request.form.get("observacoes") or "").strip()
        cur.execute(f"UPDATE registos_limpeza SET observacoes={ph} WHERE id={ph}", (nova_obs, rid))
        # Comentários da verificação
        comentarios_verificacao = (request.form.get("comentarios_verificacao") or "").strip()
        cur.execute(f"UPDATE registos_limpeza SET comentarios_verificacao={ph} WHERE id={ph}", (comentarios_verificacao, rid))
        # Anexos
        files = request.files.getlist("ficheiros")
        if files:
            day_dir = UPLOAD_DIR / now_pt().strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                if not f or f.filename == "": continue
                if not allowed_file(f.filename): continue
                fname = secure_filename(f.filename)
                path = day_dir / fname
                i = 1
                stem, suf = Path(fname).stem, Path(fname).suffix
                while path.exists():
                    path = day_dir / f"{stem}_{i}{suf}"; i += 1
                f.save(path)
                cur.execute(
                    f"INSERT INTO anexos (registo_id, caminho, tipo) VALUES ({ph}, {ph}, {ph})",
                    (rid, str(path.relative_to(BASE_DIR)), "foto" if suf.lower() != ".pdf" else "pdf")
                )
        conn.commit()
        flash("Observações, comentários e anexos atualizados.", "success")
        return redirect(url_for("registo_detalhe", rid=rid))

    cur.execute(f"""
        SELECT r.*, v.matricula, v.num_frota, p.nome as protocolo, f.nome as funcionario
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE r.id = {ph}
    """, (rid,))
    registo = cur.fetchone()
    cur.execute(f"SELECT id, caminho, tipo FROM anexos WHERE registo_id={ph} ORDER BY id", (rid,))
    anexos = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not registo:
        flash("Registo não encontrado.", "danger")
        return redirect(url_for("registos"))
    return render_template("registo_detalhe.html", registo=registo, anexos=anexos, signature=APP_SIGNATURE)

@app.route("/viaturas/<int:viatura_id>/editar", methods=["GET", "POST"])
@login_required
@require_perm("viaturas:import")
def editar_viatura(viatura_id):
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    if request.method == "POST":
        regiao = request.form.get("regiao") or None
        descricao = (request.form.get("descricao") or "").strip() or None
        verificacao_limpeza = request.form.get("verificacao_limpeza") or None
        # Só admin pode alterar a região
        if session.get("role") == "admin":
            cur.execute(
                f"UPDATE viaturas SET regiao={ph}, descricao={ph}, verificacao_limpeza={ph} WHERE id={ph}",
                (regiao, descricao, verificacao_limpeza, viatura_id)
            )
        else:
            cur.execute(
                f"UPDATE viaturas SET descricao={ph}, verificacao_limpeza={ph} WHERE id={ph}",
                (descricao, verificacao_limpeza, viatura_id)
            )
        conn.commit()
        conn.close()
        flash("Viatura atualizada.", "success")
        return redirect(url_for("viaturas"))
    cur.execute(f"SELECT * FROM viaturas WHERE id={ph}", (viatura_id,))
    viatura = cur.fetchone()
    conn.close()
    if not viatura:
        flash("Viatura não encontrada.", "danger")
        return redirect(url_for("viaturas"))
    return render_template("viatura_form.html", viatura=viatura, user_role=session.get("role"))

@app.route("/viaturas/<int:viatura_id>/apagar", methods=["POST"])
@login_required
@require_perm("viaturas:import")
def apagar_viatura(viatura_id):
    # Apenas gestor ou admin pode apagar
    if session.get("role") not in {"admin", "gestor"}:
        flash("Sem permissões para apagar viaturas.", "danger")
        return redirect(url_for("viaturas"))
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    try:
        cur.execute(f"DELETE FROM viaturas WHERE id={ph}", (viatura_id,))
        conn.commit()
        conn.close()
        flash("Viatura eliminada.", "success")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash("Não foi possível eliminar a viatura (pode estar em uso).", "danger")
    return redirect(url_for("viaturas"))
# -----------------------------------------------------------------------------
# Importação de viaturas via Dashboard (não-admin)
# -----------------------------------------------------------------------------
@app.route("/viaturas/importar", methods=["GET","POST"])
@login_required
@require_perm("viaturas:import")
def importar_viaturas():
    if request.method == "GET":
        return render_template("admin_import_viaturas.html", signature=APP_SIGNATURE)

    file = request.files.get("ficheiro")
    if not file or file.filename == "":
        flash("Selecione um ficheiro CSV.", "danger")
        return redirect(url_for("importar_viaturas"))

    raw = file.read()

    def _read_csv_text_with_encoding_guess_bytes(raw_bytes: bytes):
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return raw_bytes.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return raw_bytes.decode("latin-1", "replace"), "latin-1"

    text, enc = _read_csv_text_with_encoding_guess_bytes(raw)
    sample = text[:4096]
    delim = ";" if sample.count(";") >= sample.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        flash("CSV vazio.", "danger")
        return redirect(url_for("importar_viaturas"))

    import unicodedata, re as _re
    def _norm(h: str) -> str:
        s = (h or "").strip().lower()
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
        s = s.replace("º", "o")
        s = _re.sub(r"[^a-z0-9]+", " ", s).strip()
        aliases = {
            "mat": "matricula", "matricula": "matricula",
            "n viat": "num_frota", "no viat": "num_frota", "n viatura": "num_frota",
            "n frota": "num_frota", "num frota": "num_frota", "numero frota": "num_frota",
            "regiao": "regiao", "operacao": "operacao",
            "marca": "marca", "modelo": "modelo",
            "ativo": "ativo",
            "tipo protocolo": "tipo_protocolo",
            "tipo de protocolo": "tipo_protocolo",
            "protocolo": "tipo_protocolo",
        }
        return aliases.get(s, s)

    nh = [_norm(h) for h in header]
    wanted = ("matricula","num_frota","regiao","operacao","marca","modelo","tipo_protocolo","ativo")
    idx = {k: (nh.index(k) if k in nh else -1) for k in wanted}
    if idx["matricula"] == -1:
        flash("O CSV precisa da coluna 'Mat.' ou 'Matrícula'.", "danger")
        return redirect(url_for("importar_viaturas"))

    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    # garantir colunas
    try:
        cur.execute("PRAGMA table_info(viaturas)")
        vcols = {r["name"] for r in cur.fetchall()}
    except Exception:
        vcols = set()
    for col in ("regiao","operacao","marca","modelo"):
        if col not in vcols:
            try:
                cur.execute(f"ALTER TABLE viaturas ADD COLUMN {col} TEXT")
            except Exception:
                pass

    def _as_bool(v):
        s = (str(v or "").strip().lower())
        if s in {"1","true","t","y","yes","sim","s"}: return 1
        if s in {"0","false","f","n","no","nao","não"}: return 0
        return 1

    ins = upd = 0
    for row in reader:
        def val(key):
            i = idx[key]
            return (row[i].strip() if i != -1 and i < len(row) else "")

        m = val("matricula")
        if not m:
            continue

        num_frota = val("num_frota") or None
        regiao    = val("regiao") or None
        operacao  = val("operacao") or None
        marca     = val("marca") or None
        modelo    = val("modelo") or None
        ativo     = _as_bool(val("ativo"))
        tipo_protocolo = val("tipo_protocolo") or None

        cur.execute(f"SELECT id FROM viaturas WHERE matricula={ph}", (m,))
        ex = cur.fetchone()
        if ex:
            cur.execute(f"""
                UPDATE viaturas SET 
                  num_frota=COALESCE({ph}, num_frota),
                  regiao=COALESCE({ph}, regiao),
                  operacao=COALESCE({ph}, operacao),
                  marca=COALESCE({ph}, marca),
                  modelo=COALESCE({ph}, modelo),
                  tipo_protocolo=COALESCE({ph}, tipo_protocolo),
                  ativo={ph}
                WHERE id={ph}
            """, (num_frota, regiao, operacao, marca, modelo, tipo_protocolo, ativo, ex["id"]))
            upd += 1
        else:
            cur.execute(f"""
                INSERT INTO viaturas
                  (matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, ativo)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
            """, (m, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, ativo))
            ins += 1

    conn.commit()
    conn.close()
    flash(f"Importação concluída (encoding: {enc}): {ins} inseridas, {upd} atualizadas.", "success")
    return redirect(url_for("viaturas"))

    return render_template("admin_import_viaturas.html", signature=APP_SIGNATURE)

@app.route("/admin/viaturas/upload_csv", methods=["GET","POST"])
def admin_viaturas_upload_csv():
    return redirect(url_for("admin_import_viaturas"))

@app.route("/viaturas/<int:viatura_id>/ativar_desativar", methods=["POST"])
@login_required
@require_perm("viaturas:import")
def ativar_desativar_viatura(viatura_id):
    if session.get("role") not in {"admin", "gestor"}:
        flash("Sem permissões para alterar estado da viatura.", "danger")
        return redirect(url_for("viaturas"))
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)
    cur.execute(f"SELECT ativo FROM viaturas WHERE id={ph}", (viatura_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        flash("Viatura não encontrada.", "danger")
        return redirect(url_for("viaturas"))
    novo_estado = 0 if row["ativo"] else 1
    cur.execute(f"UPDATE viaturas SET ativo={ph} WHERE id={ph}", (novo_estado, viatura_id))
    conn.commit()
    conn.close()
    flash(f"Viatura {'ativada' if novo_estado else 'desativada'}.", "success")
    return redirect(url_for("viaturas"))

@app.route("/viaturas/exportar_excel")
@login_required
@require_perm("viaturas:view")
def exportar_viaturas_excel():
    import pandas as pd
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo, criado_em
        FROM viaturas
        ORDER BY matricula
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    df = pd.DataFrame(rows)
    fname = EXPORT_DIR / f"viaturas_{now_pt().strftime('%Y%m%d_%H%M%S')}.xlsx"
    df.to_excel(fname, index=False, sheet_name="Viaturas")
    return send_file(fname, as_attachment=True)
# -----------------------------------------------------------------------------
# Protocolos (listar / editar / novo)
# -----------------------------------------------------------------------------
@app.route("/protocolos")
@login_required
@require_perm("protocolos:view")
def protocolos():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM protocolos ORDER BY nome")
    ps = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("protocolos.html", protocolos=ps, signature=APP_SIGNATURE)

from sqlite3 import IntegrityError

def _passos_to_json(texto: str) -> str:
    passos = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    return json.dumps({"passos": passos}, ensure_ascii=False)

def _json_to_passos_text(passos_json: str) -> str:
    try:
        data = json.loads(passos_json or "{}")
        return "\n".join(data.get("passos", []))
    except Exception:
        return ""

@app.route("/protocolos/novo", methods=["GET", "POST"])
@login_required
@require_perm("protocolos:edit")
def protocolo_novo():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        freq = request.form.get("frequencia_dias")
        ativo = 1 if request.form.get("ativo") == "1" else 0
        passos_txt = request.form.get("passos", "")
        custo_limpeza = request.form.get("custo_limpeza")
        try:
            custo_limpeza = float(custo_limpeza) if custo_limpeza not in (None, "") else 25
        except Exception:
            custo_limpeza = 25

        if not nome:
            flash("Indique o nome do protocolo.", "danger")
            return redirect(url_for("protocolo_novo"))

        try:
            frequencia = int(freq) if (freq or "").strip() != "" else None
            if frequencia is not None and frequencia < 0: raise ValueError
        except ValueError:
            flash("Frequência (dias) inválida.", "danger")
            return redirect(url_for("protocolo_novo"))

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO protocolos (nome, passos_json, frequencia_dias, ativo, custo_limpeza) VALUES (?,?,?,?,?)",
                (nome, _passos_to_json(passos_txt), frequencia, ativo, custo_limpeza),
            )
            conn.commit()
            flash("Protocolo criado com sucesso.", "info")
            return redirect(url_for("protocolos"))
        except IntegrityError:
            flash("Já existe um protocolo com esse nome.", "danger")
            return redirect(url_for("protocolo_novo"))
        finally:
            conn.close()

    return render_template("protocolos_form.html", modo="novo", form={
        "nome": "", "frequencia_dias": "", "passos": "", "ativo": 1, "custo_limpeza": ""
    }, signature=APP_SIGNATURE)

@app.route("/protocolos/<int:pid>/editar", methods=["GET", "POST"])
@login_required
@require_perm("protocolos:edit")
def protocolo_editar(pid: int):
    conn = get_conn()
    cur = conn.cursor()
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        freq = request.form.get("frequencia_dias")
        ativo = 1 if request.form.get("ativo") == "1" else 0
        passos_txt = request.form.get("passos", "")
        custo_limpeza = request.form.get("custo_limpeza")
        try:
            custo_limpeza = float(custo_limpeza) if custo_limpeza not in (None, "") else 25
        except Exception:
            custo_limpeza = 25

        if not nome:
            flash("Indique o nome do protocolo.", "danger")
            conn.close()
            return redirect(url_for("protocolo_editar", pid=pid))

        try:
            frequencia = int(freq) if (freq or "").strip() != "" else None
            if frequencia is not None and frequencia < 0:
                raise ValueError
        except ValueError:
            flash("Frequência (dias) inválida.", "danger")
            conn.close()
            return redirect(url_for("protocolo_editar", pid=pid))

        try:
            cur.execute("""
                UPDATE protocolos
                   SET nome=?, passos_json=?, frequencia_dias=?, ativo=?, custo_limpeza=?
                 WHERE id=?
            """, (nome, _passos_to_json(passos_txt), frequencia, ativo, custo_limpeza, pid))
            if cur.rowcount == 0:
                flash("Protocolo não encontrado.", "danger")
            else:
                flash("Protocolo atualizado.", "info")
            conn.commit()
            return redirect(url_for("protocolos"))
        except IntegrityError:
            flash("Já existe um protocolo com esse nome.", "danger")
            return redirect(url_for("protocolo_editar", pid=pid))
        finally:
            conn.close()

    cur.execute("SELECT * FROM protocolos WHERE id=?", (pid,))
    p = cur.fetchone()
    conn.close()
    if not p:
        flash("Protocolo não encontrado.", "danger")
        return redirect(url_for("protocolos"))

    form = {
        "nome": p["nome"],
        "frequencia_dias": "" if p["frequencia_dias"] is None else int(p["frequencia_dias"]),
        "passos": _json_to_passos_text(p["passos_json"]),
        "ativo": p["ativo"],
        "custo_limpeza": p["custo_limpeza"] if "custo_limpeza" in p.keys() else ""
    }
    return render_template("protocolos_form.html", modo="editar", pid=pid, form=form, signature=APP_SIGNATURE)
# -----------------------------------------------------------------------------
# --- ADMIN: MIGRAÇÕES -------------------------------------------------------
@app.route("/admin/run_migrations")
def admin_run_migrations():
    if not session.get("is_admin"):
        return redirect(url_for("sem_permissao"))

    conn = get_conn()
    cur = conn.cursor()

    # Helper para ver colunas
    def cols(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}

    done = []

    # 1) Criar tabela protocolos (se não existir)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS protocolos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            ativo INTEGER NOT NULL DEFAULT 1
        );
    """)
    done.append("Tabela 'protocolos' OK")

    # 2) Garantir coluna protocolo_id em viaturas
    viat_cols = cols("viaturas")
    if "protocolo_id" not in viat_cols:
        cur.execute("ALTER TABLE viaturas ADD COLUMN protocolo_id INTEGER NULL;")
        done.append("Coluna 'viaturas.protocolo_id' criada")
        
        # garantir coluna 'conteudo' em protocolos
    prot_cols = cols("protocolos")
    if "conteudo" not in prot_cols:
        cur.execute("ALTER TABLE protocolos ADD COLUMN conteudo TEXT DEFAULT '' ;")
        done.append("Coluna 'protocolos.conteudo' criada")

    # 3) Migrar 'Dop' -> 'Regiao'
    viat_cols = cols("viaturas")
    has_dop = "Dop" in viat_cols
    has_regiao = "Regiao" in viat_cols

    if has_dop and not has_regiao:
        # Tentar rename nativo (SQLite >= 3.25)
        try:
            cur.execute("ALTER TABLE viaturas RENAME COLUMN Dop TO Regiao;")
            done.append("Coluna 'Dop' renomeada para 'Regiao'")
            has_dop, has_regiao = False, True
        except Exception:
            # Fallback seguro: criar Regiao, copiar valores de Dop
            cur.execute("ALTER TABLE viaturas ADD COLUMN Regiao TEXT;")
            cur.execute("""
                UPDATE viaturas SET Regiao = Dop
                WHERE (Regiao IS NULL OR TRIM(Regiao) = '')
                  AND Dop IS NOT NULL AND TRIM(Dop) <> '';
            """)
            done.append("Criada 'Regiao' e copiados valores de 'Dop' (fallback)")
            has_dop, has_regiao = True, True

    elif has_dop and has_regiao:
        # Copiar conteúdos se Regiao estiver vazia
        cur.execute("""
            UPDATE viaturas SET Regiao = Dop
            WHERE (Regiao IS NULL OR TRIM(Regiao) = '')
              AND Dop IS NOT NULL AND TRIM(Dop) <> '';
        """)
        done.append("Sincronizados valores de 'Dop' → 'Regiao'")

        # Tentar remover 'Dop' (SQLite >= 3.35) — opcional
        try:
            cur.execute("ALTER TABLE viaturas DROP COLUMN Dop;")
            done.append("Coluna 'Dop' removida")
        except Exception:
            done.append("Não foi possível remover 'Dop' (ignorado)")

    elif not has_dop and not has_regiao:
        # Nenhuma existe? Criar Regiao vazia para normalizar
        cur.execute("ALTER TABLE viaturas ADD COLUMN Regiao TEXT;")
        done.append("Coluna 'Regiao' criada (não existia 'Dop')")

    conn.commit()

    # Seed opcional de protocolos (só se estiver vazio)
    qtd = conn.execute("SELECT COUNT(*) FROM protocolos;").fetchone()[0]
    if qtd == 0:
        conn.executemany(
            "INSERT INTO protocolos (nome, ativo) VALUES (?,1);",
            [("Interior Básico",), ("Exterior Completo",), ("Desinfeção",)]
        )
        conn.commit()
        done.append("Protocolos base inseridos")

    flash("Migrações concluídas: " + " | ".join(done), "success")
    return redirect(url_for("admin_protocolos"))


# stos (lista / novo / anexos)
# -----------------------------------------------------------------------------


@app.route("/solicitar_autorizacao/<int:viatura_id>", methods=["POST"])
@login_required
def solicitar_autorizacao(viatura_id):
    funcionario_id = session.get("user_id")
    conn = get_conn()
    cur = conn.cursor()
    ph = sql_placeholder(conn)

    # Obter região, descrição e número de frota da viatura
    cur.execute("SELECT regiao, descricao, num_frota FROM viaturas WHERE id=?", (viatura_id,))
    row = cur.fetchone()
    regiao = (row["regiao"] or "").strip() if row and row["regiao"] else None
    v_desc = (row["descricao"] or "").strip() if row and row["descricao"] else ""
    num_frota = row["num_frota"] if row else None

    # Enforce de acesso: operador pode solicitar autorização apenas nas viaturas permitidas
    if session.get("role") == "operador":
        cur.execute("SELECT regiao, descricao_viaturas FROM funcionarios WHERE id=?", (funcionario_id,))
        prof = cur.fetchone()
        prof_regiao = (prof["regiao"] or "").strip() if prof and prof["regiao"] else None
        if prof_regiao:
            if regiao != prof_regiao:
                flash("Sem permissão para solicitar autorização nesta viatura.", "danger")
                conn.close()
                return redirect(url_for("novo_registo"))
        else:
            prof_desc = (prof["descricao_viaturas"] or "").strip() if prof and prof["descricao_viaturas"] else ""
            prof_desc_list = parse_descricao_viaturas(prof_desc)
            if prof_desc_list and v_desc not in prof_desc_list:
                flash("Sem permissão para solicitar autorização nesta viatura.", "danger")
                conn.close()
                return redirect(url_for("novo_registo"))

    destinatario_id = None
    if regiao:
        cur.execute("SELECT id FROM funcionarios WHERE role='gestor' AND ativo=1 AND regiao=?", (regiao,))
        gestor = cur.fetchone()
        if gestor:
            destinatario_id = gestor["id"]

    if not destinatario_id:
        flash("Não foi possível identificar o gestor destinatário para esta viatura.", "danger")
        conn.close()
        return redirect(url_for("novo_registo"))

    # Verifica se já existe pedido pendente hoje
    hoje_sql, hoje_val = sql_date_eq_today("data_pedido", conn)
    cur.execute(f"""
        SELECT 1 FROM pedidos_autorizacao
        WHERE viatura_id={ph} AND funcionario_id={ph} AND {hoje_sql} AND validado=0
    """, (viatura_id, funcionario_id, hoje_val))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO pedidos_autorizacao (viatura_id, num_frota, funcionario_id, destinatario_id) VALUES (?,?,?,?)",
            (viatura_id, num_frota, funcionario_id, destinatario_id)
        )
        conn.commit()
        flash("Pedido de autorização enviado ao gestor da região.", "info")
    else:
        flash("Já existe um pedido pendente para esta viatura hoje.", "warning")
    conn.close()
    return redirect(url_for("novo_registo"))

@app.route("/registos/novo", methods=["GET", "POST"])
@login_required
def novo_registo():
    if not user_can("registos:create"):
        flash("Sem permissões para criar registos.", "danger")
        return redirect(url_for("sem_permissao"))

    conn = get_conn()
    cur = conn.cursor()

    # Obter a região do operador (se existir)
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_operador = None
    desc_list_operador: list[str] = []
    if user_role in ("operador", "gestor"):
        cur.execute("SELECT regiao, descricao_viaturas FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_operador = (row["regiao"] or "").strip() if row and row["regiao"] else None
        if user_role == "operador" and not regiao_operador:
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            desc_list_operador = parse_descricao_viaturas(descricao_user)

    # GET: mostra o formulário
    if request.method == "GET":
        # Filtrar viaturas pela região do operador, se existir
        viaturas_sql = "SELECT id, matricula, descricao, num_frota FROM viaturas WHERE ativo=1"
        viaturas_params = []
        if regiao_operador:
            viaturas_sql += " AND regiao = ?"
            viaturas_params.append(regiao_operador)
        elif desc_list_operador:
            placeholders = ",".join(["?"] * len(desc_list_operador))
            viaturas_sql += f" AND COALESCE(descricao,'') IN ({placeholders})"
            viaturas_params.extend(desc_list_operador)
        viaturas_sql += " ORDER BY matricula"
        cur.execute(viaturas_sql, viaturas_params)
        vs = [dict(row) for row in cur.fetchall()]

        cur.execute("SELECT id, nome, passos_json, frequencia_dias FROM protocolos WHERE ativo=1 ORDER BY nome")
        ps = [dict(row) for row in cur.fetchall()]
        hoje_val = today_pt_iso()
        cur.execute("SELECT DISTINCT viatura_id FROM registos_limpeza WHERE date(data_hora) = ?", (hoje_val,))
        limpas_hoje = {r["viatura_id"] for r in cur.fetchall()}
        cur.execute("SELECT id, nome FROM funcionarios WHERE role='gestor' AND ativo=1")
        gestores = [dict(row) for row in cur.fetchall()]
        # Viaturas autorizadas a limpeza extra hoje
        cur.execute("""
            SELECT viatura_id FROM pedidos_autorizacao
            WHERE validado=1 AND date(data_pedido)=?
        """, (hoje_val,))
        viaturas_autorizadas = {r["viatura_id"] for r in cur.fetchall()}
        conn.close()
        limpa_hoje_map = {v["id"]: (v["id"] in limpas_hoje) for v in vs}
        return render_template(
            "novo_registo.html",
            viaturas=vs,
            protocolos=ps,
            limpa_hoje_map=limpa_hoje_map,
            signature=APP_SIGNATURE,
            gestores=gestores,
            viaturas_autorizadas=viaturas_autorizadas
        )

    # POST: processa o formulário
    viatura_id = request.form.get("viatura_id")
    protocolo_id = request.form.get("protocolo_id")
    estado = request.form.get("estado", "concluido")
    observacoes = (request.form.get("observacoes") or "").strip()
    local = (request.form.get("local") or "").strip()
    hora_inicio = now_pt().strftime("%H:%M")
    funcionario_id = session.get("user_id")
    hoje_val = today_pt_iso()

    if not (viatura_id and protocolo_id):
        flash("Selecione viatura e protocolo.", "danger")
        conn.close()
        return redirect(url_for("novo_registo"))

    # Enforce de acesso: operador pode criar registos apenas nas viaturas permitidas
    if user_role == "operador":
        cur.execute("SELECT regiao, descricao FROM viaturas WHERE id=?", (viatura_id,))
        vrow = cur.fetchone()
        if not vrow:
            flash("Viatura não encontrada.", "danger")
            conn.close()
            return redirect(url_for("novo_registo"))
        v_regiao = (vrow["regiao"] or "").strip()
        v_desc = (vrow["descricao"] or "").strip()
        if regiao_operador:
            if v_regiao != regiao_operador:
                flash("Sem permissão para esta viatura.", "danger")
                conn.close()
                return redirect(url_for("novo_registo"))
        elif desc_list_operador:
            if v_desc not in desc_list_operador:
                flash("Sem permissão para esta viatura.", "danger")
                conn.close()
                return redirect(url_for("novo_registo"))
    
    # Verifica se já foi limpa hoje
    cur.execute("""
        SELECT COUNT(*) FROM registos_limpeza
        WHERE viatura_id = ? AND date(data_hora) = ?
    """, (viatura_id, hoje_val))
    ja_limpo_hoje = cur.fetchone()[0] > 0

    pedido_autorizado = pedido_autorizado_hoje(viatura_id, funcionario_id)
    extra_autorizada = 1 if pedido_autorizado else 0
    responsavel_autorizacao = None
    if pedido_autorizado:
        cur.execute("""
            SELECT f.nome
            FROM pedidos_autorizacao pa
            JOIN funcionarios f ON f.id = pa.destinatario_id
            WHERE pa.viatura_id=? AND pa.funcionario_id=? AND pa.validado=1 AND date(pa.data_pedido)=?
            ORDER BY pa.data_pedido DESC LIMIT 1
        """, (viatura_id, funcionario_id, hoje_val))
        row = cur.fetchone()
        responsavel_autorizacao = row["nome"] if row else None

    # validação de horas
    def _is_hhmm(s):
        import re
        return bool(re.fullmatch(r"[0-2]\d:[0-5]\d", s))
    if hora_inicio and not _is_hhmm(hora_inicio):
        flash("Hora de início inválida (use HH:MM).", "danger")
        conn.close()
        return redirect(url_for("novo_registo"))
    
    # Se já foi limpa hoje e não tem autorização, pede autorização
    if ja_limpo_hoje and not pedido_autorizado:
        flash("Viatura já efetuou limpeza hoje, solicite autorização para limpeza extra.", "warning")
        viaturas_sql = "SELECT id, matricula, descricao, num_frota FROM viaturas WHERE ativo=1"
        viaturas_params = []
        if regiao_operador:
            viaturas_sql += " AND regiao = ?"
            viaturas_params.append(regiao_operador)
        elif desc_list_operador:
            placeholders = ",".join(["?"] * len(desc_list_operador))
            viaturas_sql += f" AND COALESCE(descricao,'') IN ({placeholders})"
            viaturas_params.extend(desc_list_operador)
        viaturas_sql += " ORDER BY matricula"
        cur.execute(viaturas_sql, viaturas_params)
        vs = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, nome FROM protocolos WHERE ativo=1 ORDER BY nome")
        ps = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT DISTINCT viatura_id FROM registos_limpeza WHERE date(data_hora) = ?", (hoje_val,))
        limpas_hoje = {r["viatura_id"] for r in cur.fetchall()}
        cur.execute("SELECT id, nome FROM funcionarios WHERE role='gestor' AND ativo=1")
        gestores = [dict(row) for row in cur.fetchall()]
        cur.execute("""
            SELECT viatura_id FROM pedidos_autorizacao
            WHERE validado=1 AND date(data_pedido)=?
        """, (hoje_val,))
        viaturas_autorizadas = {r["viatura_id"] for r in cur.fetchall()}
        conn.close()
        limpa_hoje_map = {v["id"]: (v["id"] in limpas_hoje) for v in vs}
        return render_template(
            "novo_registo.html",
            viaturas=vs,
            protocolos=ps,
            limpa_hoje_map=limpa_hoje_map,
            signature=APP_SIGNATURE,
            mostrar_botao_autorizacao=True,
            viatura_id=viatura_id,
            gestores=gestores,
            viaturas_autorizadas=viaturas_autorizadas
        )

    # inserir registo
    # Obter a região atual da viatura
    cur.execute("SELECT regiao FROM viaturas WHERE id=?", (viatura_id,))
    row = cur.fetchone()
    regiao_viatura = (row["regiao"] or "") if row else None

    cur.execute("""
        INSERT INTO registos_limpeza
        (viatura_id, protocolo_id, funcionario_id, data_hora, estado, observacoes,
        local, hora_inicio, hora_fim, extra_autorizada, responsavel_autorizacao, regiao)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        viatura_id, protocolo_id, funcionario_id,
        now_pt_iso(),
        estado, observacoes, (local or None),
        (hora_inicio or None), None,
        extra_autorizada, responsavel_autorizacao,
        regiao_viatura
    ))

    registo_id = cur.lastrowid
    # Preencher tipo_protocolo na viatura
    cur.execute("""
        UPDATE viaturas
        SET tipo_protocolo = (
            SELECT nome FROM protocolos WHERE id = ?
        )
        WHERE id = ?
    """, (protocolo_id, viatura_id))

    # anexos
    files = request.files.getlist("ficheiros")
    if files:
        day_dir = UPLOAD_DIR / now_pt().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not f or f.filename == "": continue
            if not allowed_file(f.filename): continue
            fname = secure_filename(f.filename)
            path = day_dir / fname
            i = 1
            stem, suf = Path(fname).stem, Path(fname).suffix
            while path.exists():
                path = day_dir / f"{stem}_{i}{suf}"; i += 1
            f.save(path)
            cur.execute(
                "INSERT INTO anexos (registo_id, caminho, tipo) VALUES (?, ?, ?)",
                (registo_id, str(path.relative_to(BASE_DIR)), "foto" if suf.lower() != ".pdf" else "pdf")
            )

    if pedido_autorizado:
        cur.execute("""
            DELETE FROM pedidos_autorizacao
            WHERE viatura_id=? AND funcionario_id=? AND validado=1 AND date(data_pedido)=?
        """, (viatura_id, funcionario_id, hoje_val))
    conn.commit()
    conn.close()
    flash(f"Registo #{registo_id} criado com sucesso.", "info")
    return redirect(url_for("registos"))    

def pedido_autorizado_hoje(viatura_id, funcionario_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM pedidos_autorizacao
         WHERE viatura_id=? AND funcionario_id=? AND validado=1 AND date(data_pedido)=?
    """, (viatura_id, funcionario_id, today_pt_iso()))
    res = cur.fetchone()
    conn.close()
    return bool(res)

@app.route("/registos/em_progresso")
@login_required
@require_perm("registos:view")
def registos_em_progresso():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id as registo_id, r.data_hora, v.matricula, v.num_frota,
               p.nome as protocolo, f.nome as funcionario, r.local, r.hora_inicio
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE r.estado='em_progresso' AND (r.hora_fim IS NULL OR r.hora_fim='')
        ORDER BY datetime(r.data_hora) DESC, r.id DESC
    """)
    registos = [dict(row) for row in cur.fetchall()]
    conn.close()
    return render_template("registos_em_progresso.html", registos=registos, signature=APP_SIGNATURE)

@app.route("/registos/<int:registo_id>/finalizar", methods=["POST"])
@login_required
@require_perm("registos:edit")
def finalizar_registo(registo_id):
    hora_fim = now_pt().strftime("%H:%M")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE registos_limpeza
        SET hora_fim=?, estado='concluido'
        WHERE id=?
    """, (hora_fim, registo_id))
    conn.commit()
    conn.close()
    flash("Registo finalizado.", "success")
    return redirect(url_for("registos_em_progresso"))

@app.route("/validar_limpeza/<int:viatura_id>", methods=["POST"])
@login_required
@require_perm("dashboard:view")
def validar_limpeza(viatura_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE viaturas SET limpeza_validada=1 WHERE id=?", (viatura_id,))
    conn.commit()
    conn.close()
    flash("Limpeza extra autorizada! Operador notificado.", "success")
    return redirect(url_for("home"))

@app.route("/registos/<int:rid>/apagar", methods=["POST"])
@login_required
@require_perm("registos:delete")
def registo_apagar(rid: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM registos_limpeza WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        flash("Registo eliminado.", "success")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash("Não foi possível eliminar o registo.", "danger")
    return redirect(url_for("registos"))

@app.route("/registos/<int:registo_id>/anexos")
@login_required
@require_perm("registos:view")
def ver_anexos(registo_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, caminho, tipo FROM anexos WHERE registo_id=? ORDER BY id", (registo_id,))
    anex = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("anexos.html", registo_id=registo_id, anexos=anex, signature=APP_SIGNATURE)

@app.route("/anexos/<int:anexo_id>")
@login_required
@require_perm("registos:view")
def download_anexo(anexo_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT caminho FROM anexos WHERE id=?", (anexo_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        abort(404)
    path = BASE_DIR / row["caminho"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True)

@app.route("/exportar_contabilidade_excel")
@login_required
def exportar_contabilidade_excel():
    import pandas as pd
    mes = request.args.get("mes")
    protocolo_id = request.args.get("protocolo_id")
    regiao = request.args.get("regiao")
    empresa = request.args.get("empresa")

    # Só admin pode exportar todas as regiões
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_user = None
    if user_role in ("gestor",):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT regiao FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        conn.close()
        regiao = regiao_user  # força filtro pela região do gestor

    conn = get_conn()
    cur = conn.cursor()
    sql = """
        SELECT date(r.data_hora) as data, v.matricula, v.num_frota, v.regiao, p.nome as protocolo, p.custo_limpeza, f.nome as funcionario, f.empresa, r.local
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE 1=1
    """
    params = []
    if mes:
        sql += " AND strftime('%Y-%m', r.data_hora) = ?"
        params.append(mes)
    if protocolo_id:
        sql += " AND p.id = ?"
        params.append(protocolo_id)
    if regiao:
        sql += " AND v.regiao = ?"
        params.append(regiao)
    if empresa:
        sql += " AND f.empresa = ?"
        params.append(empresa) 

    sql += " ORDER BY v.regiao ASC, date(r.data_hora) ASC, r.id ASC"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()

    # Gerar id_regiao sequencial por região (do mais antigo para o mais recente)
    if not df.empty:
        df = df.sort_values(["regiao", "data"])
        df["id_regiao"] = (
            df.groupby("regiao").cumcount() + 1
        ).apply(lambda x: f"{x:03d}")
        df["id_regiao"] = df["regiao"].fillna("—") + "-" + df["id_regiao"]
        # Ordena para exportar do mais recente para o mais antigo
        df = df.sort_values(["data"], ascending=[False])

    cols = [
        "id_regiao", "data", "matricula", "num_frota", "regiao", "protocolo",
        "custo_limpeza", "funcionario", "empresa", "local"
    ]
    df = df[cols]

    fname = EXPORT_DIR / f"contabilidade_{mes or 'todos'}_{now_pt().strftime('%Y%m%d_%H%M%S')}.xlsx"
    df.to_excel(fname, index=False, sheet_name="Contabilidade")
    return send_file(fname, as_attachment=True)
# -----------------------------------------------------------------------------
# Administração (utilizadores, perfis, import de viaturas)
# -----------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_panel():
    # Bloquear gestores
    if session.get("role") == "gestor":
        flash("Sem permissões para Administração.", "danger")
        return redirect(url_for("sem_permissao"))
    if not (user_can("users:manage") or user_can("roles:manage") or user_can("viaturas:import")):
        flash("Sem permissões para Administração.", "danger")
        return redirect(url_for("sem_permissao"))
    return render_template("admin.html", signature=APP_SIGNATURE)

@app.route("/admin/users")
@login_required
@require_perm("users:manage")
def admin_users():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, username, nome, role, regiao, ativo, criado_em FROM funcionarios ORDER BY username")
    users = [dict(r) for r in cur.fetchall()]
    conn.close()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name FROM roles ORDER BY name")
    db_roles = [r["name"] for r in cur.fetchall()]
    conn.close()
    base_roles = sorted(PERMISSIONS.keys())
    roles = sorted(set(base_roles + db_roles))
    return render_template("admin_users.html", users=users, roles=roles, signature=APP_SIGNATURE)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@require_perm("users:manage")
def admin_user_toggle(user_id):
    me = session.get("user_id")
    if user_id == me:
        flash("Não pode desativar a sua própria conta.", "warning")
        return redirect(url_for("admin_users"))
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT role, ativo FROM funcionarios WHERE id=?", (user_id,))
    u = cur.fetchone()
    if not u:
        conn.close(); flash("Utilizador não encontrado.", "danger"); return redirect(url_for("admin_users"))
    if (u["role"] or "").lower() == "admin" and u["ativo"] == 1:
        cur.execute("SELECT COUNT(*) AS n FROM funcionarios WHERE LOWER(role)='admin' AND ativo=1 AND id<>?", (user_id,))
        if cur.fetchone()["n"] == 0:
            conn.close(); flash("Não pode desativar o último admin ativo.", "danger"); return redirect(url_for("admin_users"))
    cur.execute("UPDATE funcionarios SET ativo = CASE WHEN ativo=1 THEN 0 ELSE 1 END WHERE id=?", (user_id,))
    conn.commit(); conn.close()
    flash("Estado do utilizador atualizado.", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/reset_password", methods=["GET", "POST"])
@login_required
@require_perm("users:manage")
def admin_user_reset_password(user_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, username, nome FROM funcionarios WHERE id=?", (user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        flash("Utilizador não encontrado.", "danger")
        return redirect(url_for("admin_users"))
    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        if not new_password:
            flash("A nova password é obrigatória.", "danger")
        else:
            cur.execute("UPDATE funcionarios SET password=? WHERE id=?", (generate_password_hash(new_password), user_id))
            conn.commit()
            flash("Password redefinida com sucesso.", "success")
            conn.close()
            return redirect(url_for("admin_users"))
    conn.close()
    return render_template("admin_user_reset_password.html", user=user, signature=APP_SIGNATURE)

@app.route("/admin/users/novo", methods=["GET", "POST"])
@login_required
@require_perm("users:manage")
def admin_user_new():
    roles = sorted(PERMISSIONS.keys())
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip()
        role = normalize_role(request.form.get("role"))
        ativo = 1 if request.form.get("ativo") == "1" else 0
        regiao = (request.form.get("regiao") or "").strip()
        empresa = (request.form.get("empresa") or "").strip() if role == "operador" else None
        descricao_viaturas = (request.form.get("descricao_viaturas") or "").strip() if role == "operador" else None
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username e password são obrigatórios.", "danger")
            return redirect(url_for("admin_user_new"))
        conn = get_conn(); cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO funcionarios (username, nome, role, ativo, regiao, descricao_viaturas, password, email, empresa) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, nome or username, role, ativo, regiao, descricao_viaturas, generate_password_hash(password), email, empresa)
            )
            conn.commit(); flash("Utilizador criado.", "success")
            return redirect(url_for("admin_users"))
        except sqlite3.IntegrityError:
            flash("Username já existe.", "danger")
            return redirect(url_for("admin_user_new"))
        finally:
            conn.close()
    return render_template("admin_user_form.html", roles=roles, signature=APP_SIGNATURE)

@app.route("/admin/users/<int:user_id>/editar", methods=["GET","POST"])
@login_required
@require_perm("users:manage")
def admin_user_edit(user_id):
    conn = get_conn(); cur = conn.cursor()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip()
        role = normalize_role(request.form.get("role"))
        ativo = 1 if request.form.get("ativo") == "1" else 0
        regiao = (request.form.get("regiao") or "").strip()
        empresa = (request.form.get("empresa") or "").strip() if role == "operador" else None
        descricao_viaturas = (request.form.get("descricao_viaturas") or "").strip() if role == "operador" else None
        if not username:
            flash("Username é obrigatório.", "danger"); conn.close()
            return redirect(url_for("admin_user_edit", user_id=user_id))
        try:
            cur.execute(
                "UPDATE funcionarios SET username=?, nome=?, role=?, ativo=?, regiao=?, descricao_viaturas=?, email=?, empresa=? WHERE id=?",
                (username, nome or username, role, ativo, regiao, descricao_viaturas, email, empresa, user_id)
            )
            conn.commit(); flash("Utilizador atualizado.", "info")
            return redirect(url_for("admin_users"))
        except sqlite3.IntegrityError:
            flash("Username já existe.", "danger"); conn.close()
            return redirect(url_for("admin_user_edit", user_id=user_id))
    cur.execute("SELECT * FROM funcionarios WHERE id=?", (user_id,))
    u = cur.fetchone(); conn.close()
    if not u:
        flash("Utilizador não encontrado.", "danger")
        return redirect(url_for("admin_users"))
    roles = sorted(PERMISSIONS.keys())
    return render_template("admin_user_form.html", roles=roles, user=u, signature=APP_SIGNATURE)

@app.route("/admin/roles")
@login_required
@require_perm("roles:manage")
def admin_roles():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name FROM roles ORDER BY name")
    db_roles = [r["name"] for r in cur.fetchall()]
    conn.close()
    base_roles = sorted(PERMISSIONS.keys())
    return render_template("admin_roles.html", base_roles=base_roles, db_roles=db_roles, signature=APP_SIGNATURE)

@app.route("/admin/roles/novo", methods=["GET","POST"])
@login_required
@require_perm("roles:manage")
def admin_role_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip().lower()
        perms = request.form.getlist("perms")
        if not name:
            flash("Nome do perfil obrigatório.", "danger"); return redirect(url_for("admin_role_new"))
        conn = get_conn(); cur = conn.cursor()
        try:
            cur.execute("INSERT INTO roles (name) VALUES (?)", (name,))
            role_id = cur.lastrowid
            cur.executemany("INSERT INTO role_permissions (role_id, perm) VALUES (?,?)", [(role_id, p) for p in perms])
            conn.commit(); flash("Perfil criado.", "info")
            return redirect(url_for("admin_roles"))
        except sqlite3.IntegrityError:
            flash("Esse perfil já existe.", "danger")
            return redirect(url_for("admin_role_new"))
        finally:
            conn.close()
    return render_template("admin_role_form.html", perms=KNOWN_PERMS, signature=APP_SIGNATURE)

# ...existing code...
import pandas as pd
import io
@app.route("/admin/alterar_regiao_viatura", methods=["GET", "POST"])
@login_required
def admin_alterar_regiao_viatura():
    if session.get("role") != "admin":
        flash("Apenas administradores podem alterar a região de viaturas.", "danger")
        return redirect(url_for("admin_panel"))

    conn = get_conn(); cur = conn.cursor()
    if request.method == "POST":
        viatura_id = request.form.get("viatura_id")
        nova_regiao = request.form.get("nova_regiao", "").strip()
        if viatura_id and nova_regiao:
            cur.execute("UPDATE viaturas SET regiao=? WHERE id=?", (nova_regiao, viatura_id))
            conn.commit()
            flash("Região da viatura atualizada.", "success")
        else:
            flash("Selecione uma viatura e indique a nova região.", "danger")
        conn.close()
        return redirect(url_for("admin_alterar_regiao_viatura"))

    cur.execute("SELECT id, matricula, regiao FROM viaturas ORDER BY matricula")
    viaturas = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template("admin_alterar_regiao_viatura.html", viaturas=viaturas, signature=APP_SIGNATURE)
# ...existing code...
@app.route("/admin/import/viaturas", methods=["GET","POST"])
@login_required
@require_perm("viaturas:import")
def admin_import_viaturas():
    def _str(v):
        return str(v).strip() if v is not None else None

    if request.method == "POST":
        file = request.files.get("ficheiro")
        if not file or file.filename == "":
            flash("Selecione um ficheiro CSV ou Excel.", "danger")
            return redirect(url_for("admin_import_viaturas"))
        filename = file.filename.lower()
        if filename.endswith(".xlsx"):
            df = pd.read_excel(file)
            rows = df.to_dict(orient="records")
            fieldnames = [c.lower() for c in df.columns]
        elif filename.endswith(".csv"):
            data = file.read().decode("utf-8", errors="ignore")
            reader = csv.DictReader(io.StringIO(data))
            rows = list(reader)
            fieldnames = [h.lower() for h in reader.fieldnames or []]
        else:
            flash("Ficheiro deve ser .csv ou .xlsx", "danger")
            return redirect(url_for("admin_import_viaturas"))

        required = {"matricula"}
        if not fieldnames or not required.issubset(set(fieldnames)):
            flash("Ficheiro precisa, no mínimo, da coluna 'matricula'.", "danger")
            return redirect(url_for("admin_import_viaturas"))

        conn = get_conn(); cur = conn.cursor()
        ins, upd = 0, 0
        for row in rows:
            matricula = _str(row.get("matricula") or row.get("MATRICULA"))
            if not matricula: continue
            num_frota = _str(row.get("num_frota") or row.get("NUM_FROTA"))
            regiao = _str(row.get("regiao") or row.get("REGIAO"))
            operacao = _str(row.get("operacao") or row.get("OPERACAO"))
            marca = _str(row.get("marca") or row.get("MARCA"))
            modelo = _str(row.get("modelo") or row.get("MODELO"))
            tipo_protocolo = _str(row.get("tipo_protocolo") or row.get("TIPO_PROTOCOLO"))
            descricao = _str(row.get("descricao") or row.get("DESCRICAO"))
            filial = _str(row.get("filial") or row.get("FILIAL"))
            ativo = row.get("ativo") or row.get("ATIVO")
            ativo = 1 if str(ativo).strip().lower() in {"1","true","sim","yes","y"} else 1  # default 1

            cur.execute("SELECT id FROM viaturas WHERE matricula=?", (matricula,))
            ex = cur.fetchone()
            if ex:
                cur.execute("""UPDATE viaturas
                               SET num_frota=?, regiao=?, operacao=?, marca=?, modelo=?, tipo_protocolo=?, descricao=?, filial=?, ativo=?
                               WHERE id=?""",
                            (num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo, ex["id"]))
                upd += 1
            else:
                cur.execute("""INSERT INTO viaturas (matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (matricula, num_frota, regiao, operacao, marca, modelo, tipo_protocolo, descricao, filial, ativo))
                ins += 1
        conn.commit(); conn.close()
        flash(f"Importação concluída: {ins} inseridas, {upd} atualizadas.", "info")
        return redirect(url_for("viaturas"))

    return render_template("admin_import_viaturas.html", signature=APP_SIGNATURE)


# --- UTILIZADORES: LISTAR & APAGAR -----------------------------------------
@app.route("/admin/utilizadores")
def admin_utilizadores():
    return redirect(url_for("admin_users"))

@app.route("/admin/utilizadores/delete/<int:user_id>", methods=["POST"])
def admin_utilizadores_delete(user_id):
    if not session.get("is_admin"):
        return redirect(url_for("sem_permissao"))
    conn = get_conn()
    conn.execute("DELETE FROM utilizadores WHERE id = ?;", (user_id,))
    conn.commit()
    flash("Utilizador eliminado com sucesso.", "success")
    return redirect(url_for("admin_utilizadores"))

# --- PROTOCOLOS (Separador dedicado) ----------------------------------------
@app.route("/admin/protocolos")
def admin_protocolos():
    return redirect(url_for("protocolos"))

@app.route("/admin/protocolos/new", methods=["POST"])
def admin_protocolos_new():
    if not session.get("is_admin"):
        return redirect(url_for("sem_permissao"))
    nome = request.form.get("nome", "").strip()
    conteudo = request.form.get("conteudo", "").strip()
    if not nome:
        flash("Indica um nome para o protocolo.", "warning")
        return redirect(url_for("admin_protocolos"))
    conn = get_conn()
    conn.execute("INSERT INTO protocolos (nome, conteudo, ativo) VALUES (?,?,1);",
                (nome, conteudo))
    conn.commit()
    flash("Protocolo criado.", "success")
    return redirect(url_for("admin_protocolos"))

@app.route("/admin/protocolos/<int:pid>/edit", methods=["POST"])
def admin_protocolos_edit(pid):
    if not session.get("is_admin"):
        return redirect(url_for("sem_permissao"))
    nome = request.form.get("nome", "").strip()
    conteudo = request.form.get("conteudo", "").strip()
    ativo = 1 if request.form.get("ativo") == "on" else 0
    conn = get_conn()
    conn.execute("UPDATE protocolos SET nome=?, conteudo=?, ativo=? WHERE id=?;",
                (nome, conteudo, ativo, pid))
    conn.commit()
    flash("Protocolo atualizado.", "success")
    return redirect(url_for("admin_protocolos"))

@app.route("/protocolos/<int:pid>/apagar", methods=["POST"])
@login_required
@require_perm("protocolos:edit")
def protocolo_apagar(pid: int):
    conn = get_conn()
    cur = conn.cursor()
    try:
        # Limpar referências ao protocolo nas viaturas
        cur.execute("UPDATE viaturas SET tipo_protocolo=NULL WHERE tipo_protocolo IN (SELECT nome FROM protocolos WHERE id=?)", (pid,))
        # Apagar o protocolo
        cur.execute("DELETE FROM protocolos WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        flash("Protocolo eliminado.", "success")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash("Não foi possível eliminar o protocolo (pode estar em uso).", "danger")
    return redirect(url_for("protocolos"))

@app.route("/contabilidade")
@login_required
def contabilidade():
    if session.get("role") not in {"admin", "gestor"}:
        flash("Sem permissões para aceder à contabilidade.", "danger")
        return redirect(url_for("home"))

    mes = request.args.get("mes")
    protocolo_id = request.args.get("protocolo_id")
    regiao = request.args.get("regiao")
    empresa = request.args.get("empresa")

    # Só admin pode ver todas as regiões
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_user = None
    if user_role == "gestor":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT regiao FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        conn.close()
        regiao = regiao_user  # força filtro pela região do gestor

    conn = get_conn()
    cur = conn.cursor()
    sql = """
        SELECT r.id as registo_id, date(r.data_hora) as data, 
               COALESCE(r.regiao, v.regiao) as regiao, 
               v.matricula, v.num_frota,
               p.nome as protocolo, p.custo_limpeza, f.nome as funcionario, f.empresa, r.local
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE 1=1
    """
    params = []
    if mes:
        sql += " AND strftime('%Y-%m', r.data_hora) = ?"
        params.append(mes)
    if protocolo_id:
        sql += " AND p.id = ?"
        params.append(protocolo_id)
    if regiao:
        sql += " AND (COALESCE(r.regiao, v.regiao) = ?)"
        params.append(regiao)
    if empresa:
        sql += " AND f.empresa = ?"
        params.append(empresa)
    sql += " ORDER BY regiao ASC, datetime(r.data_hora) ASC, r.id ASC"
    cur.execute(sql, params)
    registos = [dict(row) for row in cur.fetchall()]

    # Gerar id_regiao sequencial por região (do mais antigo para o mais recente)
    from collections import defaultdict
    counters = defaultdict(int)
    for r in registos:
        regiao_val = r.get("regiao") or "—"
        counters[regiao_val] += 1
        r["id_regiao"] = f"{regiao_val}-{counters[regiao_val]:03d}"
    # Para apresentação, mostra do mais recente para o mais antigo
    registos = sorted(registos, key=lambda r: (r.get("regiao") or "—", r["data"], r["registo_id"]), reverse=True)

    # Obter lista de empresas para o filtro
    cur.execute("SELECT DISTINCT empresa FROM funcionarios WHERE empresa IS NOT NULL AND TRIM(empresa)<>'' ORDER BY empresa")
    empresas = [r["empresa"] for r in cur.fetchall()]

    cur.execute("SELECT id, nome FROM protocolos WHERE ativo=1 ORDER BY nome")
    protocolos = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT regiao FROM registos_limpeza WHERE regiao IS NOT NULL AND TRIM(regiao)<>'' ORDER BY regiao")
    regioes = [r["regiao"] for r in cur.fetchall()]
    conn.close()

    total = sum(r["custo_limpeza"] or 0 for r in registos)

    return render_template(
        "contabilidade.html",
        registos=registos,
        protocolos=protocolos,
        regioes=regioes,
        empresas=empresas,
        mes=mes,
        protocolo_id=protocolo_id,
        regiao=regiao,
        empresa=empresa,
        total=total,
        signature=APP_SIGNATURE
    )

@app.route("/registos")
@login_required
@require_perm("registos:view")
def registos():
    mes = request.args.get("mes")
    conn = get_conn()
    cur = conn.cursor()

    # Obter região do utilizador (operador ou gestor)
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_user = None
    desc_list_user: list[str] = []
    if user_role in ("operador", "gestor"):
        cur.execute("SELECT regiao, descricao_viaturas FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        if user_role == "operador" and not regiao_user:
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            desc_list_user = parse_descricao_viaturas(descricao_user)

    sql = """
        SELECT r.id as registo_id, r.data_hora, r.hora_inicio, r.hora_fim, v.matricula, v.num_frota,
               p.nome as protocolo, f.nome as funcionario, r.local, r.verificacao_limpeza,
               r.extra_autorizada, v.regiao, r.observacoes
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE 1=1
    """
    params = []
    if mes:
        sql += " AND strftime('%Y-%m', r.data_hora) = ?"
        params.append(mes)
    if regiao_user:
        sql += " AND v.regiao = ?"
        params.append(regiao_user)
    elif user_role == "operador" and desc_list_user:
        placeholders = ",".join(["?"] * len(desc_list_user))
        sql += f" AND COALESCE(v.descricao,'') IN ({placeholders})"
        params.extend(desc_list_user)
    sql += " ORDER BY v.regiao ASC, datetime(r.data_hora) ASC, r.id ASC"
    cur.execute(sql, params)
    registos = [dict(row) for row in cur.fetchall()]
    conn.close()

    # Gerar ID sequencial por região (do mais antigo para o mais recente)
    from collections import defaultdict
    counters = defaultdict(int)
    for r in registos:
        regiao = r.get("regiao") or "—"
        counters[regiao] += 1
        r["id_regiao"] = f"{regiao}-{counters[regiao]:03d}"

    # Agora apresenta do mais recente para o mais antigo
    registos = sorted(registos, key=lambda r: (r["data_hora"], r["registo_id"]), reverse=True)

    return render_template("registos.html", registos=registos, mes=mes, signature=APP_SIGNATURE)
# -----------------------------------------------------------------------------
# Export Excel
# -----------------------------------------------------------------------------
@app.route("/export/excel")
@login_required
@require_perm("export:excel")
def export_excel():
    import pandas as pd
    mes = request.args.get("mes")
    conn = get_conn()
    cur = conn.cursor()

    # Obter região do utilizador (operador ou gestor)
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_user = None
    desc_list_user: list[str] = []
    if user_role in ("operador", "gestor"):
        cur.execute("SELECT regiao, descricao_viaturas FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        if user_role == "operador" and not regiao_user:
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            desc_list_user = parse_descricao_viaturas(descricao_user)

    sql = """
        SELECT
            r.id as id_regiao,
            r.data_hora,
            v.matricula,
            v.num_frota,
            p.nome as protocolo,
            f.nome as funcionario,
            r.local,
            r.estado,
            r.observacoes,
            r.hora_inicio,
            r.hora_fim,
            r.extra_autorizada,
            r.verificacao_limpeza,
            r.comentarios_verificacao,
            v.regiao
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE 1=1
    """
    params = []
    if mes:
        sql += " AND strftime('%Y-%m', r.data_hora) = ?"
        params.append(mes)
    if regiao_user and user_role != "admin":
        sql += " AND v.regiao = ?"
        params.append(regiao_user)
    elif user_role == "operador" and desc_list_user:
        placeholders = ",".join(["?"] * len(desc_list_user))
        sql += f" AND COALESCE(v.descricao,'') IN ({placeholders})"
        params.extend(desc_list_user)
    sql += " ORDER BY datetime(r.data_hora) DESC, r.id DESC"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
     
    if not df.empty:
        # Ordena por regiao e data/hora ASC (mais antigo primeiro)
        df = df.sort_values(["regiao", "data_hora", "id_regiao"])
        # Gera o ID sequencial por regiao
        df["id_regiao"] = (
            df.groupby("regiao").cumcount() + 1
        ).apply(lambda x: f"{x:03d}")
        df["id_regiao"] = df["regiao"].fillna("—") + "-" + df["id_regiao"]
        # Agora ordena para exportar do mais recente para o mais antigo
        df = df.sort_values(["data_hora", "id_regiao"], ascending=[False, False])
        df["data"] = pd.to_datetime(df["data_hora"]).dt.date
        # Normalizar campo de verificação
        df['verificacao_limpeza'] = df['verificacao_limpeza'].apply(
            lambda x: "Conforme" if str(x).strip().lower() == "conforme"
            else ("Não conforme" if str(x).strip().lower() in {"não conforme", "nao conforme"} else "")
        )
        # Calcular tempo de limpeza (em minutos)
        def calc_dur(row):
            try:
                if row['hora_inicio'] and row['hora_fim']:
                    d = pd.to_datetime(row['data_hora']).date()
                    t1 = pd.to_datetime(f"{d} {row['hora_inicio']}:00")
                    t2 = pd.to_datetime(f"{d} {row['hora_fim']}:00")
                    return max(0, int((t2 - t1).total_seconds() // 60))
            except Exception:
                pass
            return None

        df['tempo_limpeza_min'] = df.apply(calc_dur, axis=1)
        df['tipo_limpeza'] = df['extra_autorizada'].apply(lambda x: "Extra" if x == 1 else "Normal")

        # Reorganizar colunas
        cols = [
            "id_regiao", "data", "matricula", "num_frota", "protocolo",
            "funcionario", "local", "estado", "observacoes",
            "hora_inicio", "hora_fim", "tempo_limpeza_min", "tipo_limpeza", "verificacao_limpeza", "comentarios_verificacao"
        ]
        df = df[cols]

    fname = EXPORT_DIR / f"registos_limpeza_{mes or 'todos'}_{now_pt().strftime('%Y%m%d_%H%M%S')}.xlsx"

    # Sheet principal: registos
    # Sheet secundária: protocolos
    conn = get_conn()
    df_protocolos = pd.read_sql_query("""
        SELECT nome, passos_json, frequencia_dias
        FROM protocolos
        WHERE ativo=1
        ORDER BY nome
    """, conn)
    conn.close()

    # Transformar passos_json em texto
    def passos_text(row):
        try:
            data = json.loads(row['passos_json'] or '{}')
            return "\n".join(data.get('passos', []))
        except Exception:
            return ""
    df_protocolos['passos'] = df_protocolos.apply(passos_text, axis=1)
    df_protocolos = df_protocolos[['nome', 'passos', 'frequencia_dias']]

    with pd.ExcelWriter(fname, engine="xlsxwriter") as writer:
        if not df.empty:
            df.to_excel(writer, index=False, sheet_name="Registos de Limpeza")
        df_protocolos.to_excel(writer, index=False, sheet_name="Protocolos")

    return send_file(fname, as_attachment=True)

@app.route("/export/registos_excel")
@login_required
@require_perm("export:excel")
def export_registos_excel():
    import pandas as pd
    mes = request.args.get("mes")
    conn = get_conn()
    cur = conn.cursor()

    # Obter região do utilizador (operador ou gestor)
    user_id = session.get("user_id")
    user_role = session.get("role")
    regiao_user = None
    desc_list_user: list[str] = []
    if user_role in ("operador", "gestor"):
        cur.execute("SELECT regiao, descricao_viaturas FROM funcionarios WHERE id=?", (user_id,))
        row = cur.fetchone()
        regiao_user = (row["regiao"] or "").strip() if row and row["regiao"] else None
        if user_role == "operador" and not regiao_user:
            descricao_user = (row["descricao_viaturas"] or "").strip() if row and row["descricao_viaturas"] else ""
            desc_list_user = parse_descricao_viaturas(descricao_user)

    sql = """
        SELECT
            r.id as id_regiao,
            r.data_hora,
            v.matricula,
            v.num_frota,
            p.nome as protocolo,
            f.nome as funcionario,
            r.local,
            r.estado,
            r.observacoes,
            r.hora_inicio,
            r.hora_fim,
            r.extra_autorizada,
            r.verificacao_limpeza,
            r.comentarios_verificacao,
            v.regiao
        FROM registos_limpeza r
        JOIN viaturas v ON v.id = r.viatura_id
        JOIN protocolos p ON p.id = r.protocolo_id
        JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE 1=1
    """
    params = []
    if mes:
        sql += " AND strftime('%Y-%m', r.data_hora) = ?"
        params.append(mes)
    if regiao_user and user_role != "admin":
        sql += " AND v.regiao = ?"
        params.append(regiao_user)
    elif user_role == "operador" and desc_list_user:
        placeholders = ",".join(["?"] * len(desc_list_user))
        sql += f" AND COALESCE(v.descricao,'') IN ({placeholders})"
        params.extend(desc_list_user)
    sql += " ORDER BY datetime(r.data_hora) DESC, r.id DESC"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()

    if not df.empty:
        # Ordena por regiao e data/hora ASC (mais antigo primeiro)
        df = df.sort_values(["regiao", "data_hora", "id_regiao"])
        # Gera o ID sequencial por regiao
        df["id_regiao"] = (
            df.groupby("regiao").cumcount() + 1
        ).apply(lambda x: f"{x:03d}")
        df["id_regiao"] = df["regiao"].fillna("—") + "-" + df["id_regiao"]
        # Agora ordena para exportar do mais recente para o mais antigo
        df = df.sort_values(["data_hora", "id_regiao"], ascending=[False, False])
        df["data"] = pd.to_datetime(df["data_hora"]).dt.date

        # Calcular tempo de limpeza (em minutos)
        def calc_dur(row):
            try:
                if row['hora_inicio'] and row['hora_fim']:
                    d = pd.to_datetime(row['data_hora']).date()
                    t1 = pd.to_datetime(f"{d} {row['hora_inicio']}:00")
                    t2 = pd.to_datetime(f"{d} {row['hora_fim']}:00")
                    return max(0, int((t2 - t1).total_seconds() // 60))
            except Exception:
                pass
            return None

        df['tempo_limpeza_min'] = df.apply(calc_dur, axis=1)
        df['tipo_limpeza'] = df['extra_autorizada'].apply(lambda x: "Extra" if x == 1 else "Normal")

        # Reorganizar colunas
        cols = [
            "id_regiao", "data", "matricula", "num_frota", "protocolo",
            "funcionario", "local", "estado", "observacoes",
            "hora_inicio", "hora_fim", "tempo_limpeza_min", "tipo_limpeza", "verificacao_limpeza", "comentarios_verificacao"
        ]
        df = df[cols]

    fname = EXPORT_DIR / f"registos_limpeza_{mes or 'todos'}_{now_pt().strftime('%Y%m%d_%H%M%S')}.xlsx"

    # Sheet principal: registos
    # Sheet secundária: protocolos
    conn = get_conn()
    df_protocolos = pd.read_sql_query("""
        SELECT nome, passos_json, frequencia_dias
        FROM protocolos
        WHERE ativo=1
        ORDER BY nome
    """, conn)
    conn.close()

    # Transformar passos_json em texto
    def passos_text(row):
        try:
            data = json.loads(row['passos_json'] or '{}')
            return "\n".join(data.get('passos', []))
        except Exception:
            return ""
    df_protocolos['passos'] = df_protocolos.apply(passos_text, axis=1)
    df_protocolos = df_protocolos[['nome', 'passos', 'frequencia_dias']]

    with pd.ExcelWriter(fname, engine="xlsxwriter") as writer:
        if not df.empty:
            df.to_excel(writer, index=False, sheet_name="Registos de Limpeza")
        df_protocolos.to_excel(writer, index=False, sheet_name="Protocolos")

    return send_file(fname, as_attachment=True)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)