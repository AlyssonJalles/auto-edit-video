#!/usr/bin/env python
import argparse
import os
import subprocess
import sys
import re

import whisper
import pysubs2

# Import opcional da correção ADK
try:
    from adk_correction import corrigir_palavras_com_adk
except ImportError as e:
    print(f"⚠️  Aviso: Não foi possível importar o módulo de correção ADK: {e}")
    corrigir_palavras_com_adk = None


def transcrever(video_path: str, model_name: str = "small", language: str = "pt"):
    print(f"[1/3] Carregando modelo Whisper ({model_name})...")
    model = whisper.load_model(model_name)

    print(f"[2/3] Transcrevendo áudio de {video_path}...")
    # Precisamos ativar word_timestamps para ter o tempo de cada palavra
    result = model.transcribe(
        video_path,
        language=language,
        verbose=True,
        word_timestamps=True
    )
    return result["segments"]


import json

def salvar_segmentos_json(segments, json_path: str):
    """Salva os segmentos em arquivo JSON para edição posterior"""
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
        print(f"Segmentos salvos em: {json_path}")
    except Exception as e:
        print(f"Erro ao salvar JSON de segmentos: {e}")

def carregar_segmentos_json(json_path: str):
    """Carrega segmentos de arquivo JSON"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Erro ao carregar JSON de segmentos: {e}")
        return []

def interpolate_words(text, start, end):
    """
    Se o segmento não tiver 'words' (foi editado manualmente),
    quebra o texto em palavras e distribui o tempo uniformemente.
    """
    words = text.strip().split()
    if not words:
        return []
    
    duration = end - start
    per_word = duration / len(words)
    
    result = []
    current_start = start
    
    for w in words:
        w_end = current_start + per_word
        result.append({
            "word": w,
            "start": current_start,
            "end": w_end
        })
        current_start = w_end
        
    return result

def _hex_to_rgb(c):
    """Converte #RRGGBB para (r, g, b) 0-255."""
    if c and c.startswith('#') and len(c) == 7:
        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
    return (255, 255, 255)


def gerar_ass_capcut(segments, ass_path: str, highlight_color=None, text_color=None, outline_color=None, highlight_width=5.0, outline_width=1.5, font_name="Prohibition", font_size=10, sub_x_percent=None, sub_y_percent=None, play_res_x=640, play_res_y=360, subtitle_model="highlights", font_opacity=100, font_weight="bold", font_case="Tt", bg_enabled=False, bg_color=None, bg_opacity=50):
    """
    Gera um arquivo .ass com legendas dinâmicas, RESPEITANDO OS SEGMENTOS.
    sub_x_percent, sub_y_percent: 0-100, posição da legenda (centro do bloco). Se None, usa estilo padrão (marginv, alignment).
    play_res_x, play_res_y: resolução do script para \\pos (usado quando sub_x_percent/sub_y_percent são fornecidos).
    subtitle_model: "traditional" (sem highlight), "highlights" (karaokê com borda/destaque), "highlights2" (palavra a palavra na cor Destaque da fonte).
    font_opacity: 0-100. font_weight: normal, bold, italic. font_case: TT, Tt, tt.
    bg_enabled: ativa plano de fundo atrás do texto. bg_color: #RRGGBB. bg_opacity: 0-100 transparência (padrão 50% quando ativado).
    """
    print(f"[3/3] Gerando arquivo de legenda ASS em {ass_path}...")

    subs = pysubs2.SSAFile()
    if play_res_x and play_res_y:
        subs.info["PlayResX"] = str(play_res_x)
        subs.info["PlayResY"] = str(play_res_y)

    # CORES E ESTILOS (Ajuste Fino)
    HIGHLIGHT_COLOR = highlight_color if highlight_color else "&H0045FF&"
    BLACK_COLOR = outline_color if outline_color else "&H000000&"
    WHITE_COLOR = text_color if text_color else "&HFFFFFF&"
    
    def to_ass_color(c):
        if c and c.startswith('#') and len(c) == 7:
            r = c[1:3]
            g = c[3:5]
            b = c[5:7]
            return f"&H{b}{g}{r}&"
        return c

    HIGHLIGHT_COLOR = to_ass_color(HIGHLIGHT_COLOR)
    BLACK_COLOR = to_ass_color(BLACK_COLOR)
    WHITE_COLOR = to_ass_color(WHITE_COLOR)

    # Opacidade: ASS alpha 00=opaco, FF=transparente. font_opacity 100% -> alpha 0
    try:
        opacity_pct = float(font_opacity) if font_opacity is not None else 100
    except (TypeError, ValueError):
        opacity_pct = 100
    opacity_pct = max(0, min(100, opacity_pct))
    primary_alpha = int((100 - opacity_pct) * 255 / 100)
    # Plano de fundo: ativado por checkbox. Quando ativo, default preto 50% transparência.
    use_back_box = bool(bg_enabled)
    if use_back_box:
        try:
            bg_opct = float(bg_opacity) if bg_opacity is not None else 50
        except (TypeError, ValueError):
            bg_opct = 50
        bg_opct = max(0, min(100, bg_opct))
        back_alpha = int(bg_opct * 255 / 100)
    else:
        back_alpha = 255
    br, bg, bb = _hex_to_rgb(bg_color) if bg_color else (0, 0, 0)

    tr, tg, tb = _hex_to_rgb(text_color) if text_color else (255, 255, 255)
    or_, og, ob = _hex_to_rgb(outline_color) if outline_color else (0, 0, 0)

    # Traço (borda): sempre BorderStyle=1 para outline consistente; outline_width e outline_color no estilo e nos overrides
    outline_w = float(outline_width) if outline_width is not None else 1.5
    BORDER_NORMAL = outline_w
    BORDER_HIGHLIGHT = float(highlight_width)
    BLUR_HIGHLIGHT = 2.0

    # Estilo Base
    style = pysubs2.SSAStyle()
    style.fontname = font_name
    style.fontsize = font_size
    style.bold = (font_weight == "bold")
    style.italic = (font_weight == "italic")
    style.primarycolor = pysubs2.Color(tr, tg, tb, primary_alpha)
    style.outlinecolor = pysubs2.Color(or_, og, ob, 0)
    style.outline = outline_w
    style.alignment = 2   # centro inferior
    style.marginv = 95
    style.borderstyle = 1   # sempre 1 para traço (borda) funcionar corretamente
    style.shadow = 4 if use_back_box else 0
    style.backcolor = pysubs2.Color(br, bg, bb, back_alpha) if use_back_box else pysubs2.Color(0, 0, 0, 255)
    if use_back_box:
        style.borderstyle = 3   # caixa atrás do texto
        style.shadow = 4

    subs.styles["Default"] = style

    def sec_to_ms(t):
        return int(t * 1000)

    BORDER_NORMAL = outline_w
    BORDER_HIGHLIGHT = float(highlight_width)
    BLUR_HIGHLIGHT = 2.0
    
    # Traço (borda) aplicado explicitamente nos overrides para funcionar em todos os modelos
    TRACO_TAG = rf"{{\bord{BORDER_NORMAL}}}{{\3c{BLACK_COLOR}}}{{\blur0}}"
    HIGHLIGHT_TAG = rf"{{\1c{WHITE_COLOR}}}{{\3c{HIGHLIGHT_COLOR}}}{{\bord{BORDER_HIGHLIGHT}}}{{\blur{BLUR_HIGHLIGHT}}}"
    NORMAL_TAG = rf"{{\1c{WHITE_COLOR}}}{{\3c{BLACK_COLOR}}}{{\bord{BORDER_NORMAL}}}{{\blur0}}"
    # Legenda 2: palavra atual na cor "Destaque da fonte" (só cor do texto, sem borda grossa)
    HIGHLIGHT_COLOR_TEXT_TAG = rf"{{\1c{HIGHLIGHT_COLOR}}}"

    def add_pos(text, x_pct, y_pct):
        if sub_x_percent is not None and sub_y_percent is not None:
            pos_x = int((float(x_pct) / 100.0) * play_res_x)
            pos_y = int((float(y_pct) / 100.0) * play_res_y)
            return f"{{\\pos({pos_x},{pos_y})}}" + text
        return text

    def apply_case(s, case):
        if case == "TT":
            return s.upper()
        if case == "tt":
            return s.lower()
        return s.title() if s else s

    # ITERA SOBRE OS SEGMENTOS (RESPEITANDO A EDIÇÃO)
    for seg in segments:
        seg_words = seg.get("words", [])
        if not seg_words:
            seg_words = interpolate_words(seg["text"], seg["start"], seg["end"])
        if not seg_words:
            continue

        raw_words = [w["word"].strip() for w in seg_words]
        chunk_texts = [apply_case(w, font_case) for w in raw_words]
        seg_start_ms = sec_to_ms(seg["start"])
        seg_end_ms = sec_to_ms(seg["end"])

        if subtitle_model == "traditional":
            # Padrão: uma linha por segmento, sem nenhum highlight; traço explícito no override
            plain_text = " ".join(chunk_texts)
            final_text = add_pos(TRACO_TAG + plain_text, sub_x_percent, sub_y_percent)
            event = pysubs2.SSAEvent(start=seg_start_ms, end=seg_end_ms, text=final_text, style="Default")
            subs.events.append(event)

        elif subtitle_model == "highlights2":
            # Legenda 2: cada palavra falada na cor "Destaque da fonte" (Estilos), timing palavra a palavra
            for j, word_obj in enumerate(seg_words):
                w_start = sec_to_ms(word_obj["start"])
                w_end = sec_to_ms(word_obj["end"])
                if w_end > seg_end_ms:
                    w_end = seg_end_ms
                display_parts = []
                for k, text_part in enumerate(chunk_texts):
                    if k == j:
                        display_parts.append(f"{HIGHLIGHT_COLOR_TEXT_TAG}{text_part}{NORMAL_TAG}")
                    else:
                        display_parts.append(text_part)
                final_text = " ".join(display_parts)
                final_text = add_pos(final_text, sub_x_percent, sub_y_percent)
                if j < len(seg_words) - 1:
                    next_start = sec_to_ms(seg_words[j + 1]["start"])
                    if next_start - w_end < 500:
                        w_end = next_start
                event = pysubs2.SSAEvent(start=w_start, end=w_end, text=final_text, style="Default")
                subs.events.append(event)

        else:
            # highlights (Legenda 1): karaokê palavra a palavra
            for j, word_obj in enumerate(seg_words):
                w_start = sec_to_ms(word_obj["start"])
                w_end = sec_to_ms(word_obj["end"])
                if w_end > seg_end_ms:
                    w_end = seg_end_ms
                display_parts = []
                for k, text_part in enumerate(chunk_texts):
                    if k == j:
                        display_parts.append(f"{HIGHLIGHT_TAG}{text_part}{NORMAL_TAG}")
                    else:
                        display_parts.append(text_part)
                final_text = " ".join(display_parts)
                final_text = add_pos(final_text, sub_x_percent, sub_y_percent)
                if j < len(seg_words) - 1:
                    next_start = sec_to_ms(seg_words[j + 1]["start"])
                    if next_start - w_end < 500:
                        w_end = next_start
                event = pysubs2.SSAEvent(start=w_start, end=w_end, text=final_text, style="Default")
                subs.events.append(event)

    subs.save(ass_path)
    print("Legenda .ass criada.")


def queimar_legenda(video_path: str, ass_path: str, output_path: str):
    print(f"Renderizando vídeo final com ffmpeg → {output_path}")

    ass_norm = os.path.abspath(ass_path).replace("\\", "/")
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", f"subtitles='{ass_norm}'",
        "-c:a", "copy",
        output_path,
    ]

    print("Comando:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print("Erro ao rodar ffmpeg:", e)
        sys.exit(1)

def regroup_words_into_segments(words, max_chars=80, max_duration=7.0, min_gap=0.5):
    """
    Reagrupa palavras em segmentos menores baseados em:
    - Comprimento máximo de caracteres (max_chars)
    - Duração máxima (max_duration)
    - Pausa entre palavras (min_gap)
    """
    if not words:
        return []

    segments = []
    current_segment = {
        "start": words[0]["start"],
        "end": words[0]["end"],
        "words": [],
        "text": ""
    }
    
    last_end = words[0]["start"]

    for w in words:
        w_start = w["start"]
        w_end = w["end"]
        w_text = w["word"]
        
        # Calcula gap em relação à palavra anterior no loop
        gap = w_start - last_end
        
        # Decisão de quebra
        should_break = False
        
        # 1. Gap grande (silêncio)
        if gap > min_gap and len(current_segment["words"]) > 0:
            should_break = True
            
        # 2. Tamanho do texto excedido
        current_len = len(current_segment["text"]) + len(w_text) + 1
        if current_len > max_chars:
            should_break = True
            
        # 3. Duração excessiva do segmento
        seg_duration = w_end - current_segment["start"]
        if seg_duration > max_duration:
            should_break = True

        if should_break:
            # Finaliza segmento anterior
            segments.append(current_segment)
            # Inicia novo
            current_segment = {
                "start": w_start,
                "end": w_end,
                "words": [w],
                "text": w_text
            }
        else:
            # Adiciona ao atual
            current_segment["words"].append(w)
            current_segment["end"] = w_end
            if current_segment["text"]:
                current_segment["text"] += " " + w_text
            else:
                current_segment["text"] = w_text
        
        last_end = w_end

    # Adiciona o último
    if current_segment["words"]:
        segments.append(current_segment)

    return segments

def processar_legenda_completo(video_path, output_path, model_name="small", language="pt", gemini_key=None,
                               highlight_color=None, text_color=None, outline_color=None, highlight_width=5.0, outline_width=1.5, font_name="Prohibition", font_size=10,
                               subtitle_model="highlights", sub_x_percent=None, sub_y_percent=None, play_res_x=640, play_res_y=360,
                               font_opacity=100, font_weight="bold", font_case="Tt", bg_enabled=False, bg_color=None, bg_opacity=50,
                               only_generate=False):
    """
    Pipeline completo: Transcrever -> (Corrigir IA) -> Gerar ASS -> Queimar
    Se only_generate=True, para após gerar o ASS e salva JSON.
    """
    base, ext = os.path.splitext(video_path)
    ass_path = f"{base}.ass"
    json_path = f"{base}.json"

    # 1. Transcrever
    segments = transcrever(video_path, model_name=model_name, language=language)
    
    # 2. Corrigir (se solicitado)
    # Se gemini_key for passada ou None (confiando no env), e o módulo existir
    if corrigir_palavras_com_adk:
        # Verifica se deve tentar (chave explicita ou env implícito)
        should_try = False
        if gemini_key:
            should_try = True
        elif os.path.exists(".env") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            should_try = True
            
        if should_try:
            print("\n[Auto Caption] Tentando correção com IA...")
            all_words = []
            for seg in segments:
                if "words" in seg:
                    all_words.extend(seg["words"])
            
            if all_words:
                try:
                    corrected_words = corrigir_palavras_com_adk(all_words, gemini_key)
                    if corrected_words:
                        # Reconstrói estrutura para o gerador, mas segmentado
                        segments = regroup_words_into_segments(corrected_words)
                except Exception as e:
                    print(f"❌ Falha na correção IA: {e}. Usando original.")
        else:
            print("[Auto Caption] Pulando correção IA (sem chave ou não solicitada).")
            if not corrigir_palavras_com_adk:
                print("⚠️  Módulo 'adk_correction' não carregado corretamente.")

    # Salva JSON dos segmentos para edição futura
    salvar_segmentos_json(segments, json_path)

    # 3. Gerar ASS
    gerar_ass_capcut(segments, ass_path, highlight_color, text_color, outline_color, highlight_width, outline_width, font_name, font_size,
                     sub_x_percent=sub_x_percent, sub_y_percent=sub_y_percent, play_res_x=play_res_x, play_res_y=play_res_y, subtitle_model=subtitle_model,
                     font_opacity=font_opacity, font_weight=font_weight, font_case=font_case, bg_enabled=bg_enabled, bg_color=bg_color, bg_opacity=bg_opacity)
    
    if only_generate:
        print("Apenas geração solicitada. Parando antes de queimar.")
        return ass_path

    # 4. Queimar
    queimar_legenda(video_path, ass_path, output_path)
    
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Gera legenda estilo CapCut automaticamente com Whisper e queima no vídeo."
    )
    parser.add_argument("video", help="Caminho do arquivo de vídeo de entrada")
    parser.add_argument(
        "--model",
        default="small",
        help="Modelo Whisper (tiny, base, small, medium, large). Padrão: small",
    )
    parser.add_argument(
        "--language",
        default="pt",
        help="Código do idioma da fala (ex: pt, en, es). Padrão: pt",
    )
    parser.add_argument(
        "--output",
        help="Nome do vídeo de saída (opcional). Se não passar, cria <nome>_legendado.mp4",
    )
    parser.add_argument(
        "--gemini-key",
        help="API Key do Google Gemini para correção de texto.",
    )

    args = parser.parse_args()

    video_path = args.video
    if not os.path.isfile(video_path):
        print(f"Arquivo não encontrado: {video_path}")
        sys.exit(1)

    base, ext = os.path.splitext(video_path)
    output_path = args.output or f"{base}_legendado.mp4"

    processar_legenda_completo(
        video_path, 
        output_path, 
        model_name=args.model, 
        language=args.language,
        gemini_key=args.gemini_key
    )

    print("\n✅ Pronto!")
    print(f"Vídeo legendado salvo em: {output_path}")


if __name__ == "__main__":
    main()
