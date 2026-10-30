import re
from decimal import Decimal

from django import forms
from django.conf import settings

from .models import Empresa, UnidadeConsumidora
from .seguranca import ASSINATURA_PDF


class SeletorMultiplosArquivos(forms.ClearableFileInput):
    allow_multiple_selected = True


class CampoMultiplosArquivos(forms.FileField):
    """FileField que aceita varios arquivos e valida cada um."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", SeletorMultiplosArquivos(attrs={
            "accept": "application/pdf",
            "multiple": True,
        }))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        limpar_um = super().clean
        if isinstance(data, (list, tuple)):
            return [limpar_um(item, initial) for item in data]
        return [limpar_um(data, initial)]


class UploadFaturasForm(forms.Form):
    """
    Recebe PDFs de fatura enviados pelo painel.

    A validacao aqui e a primeira barreira (rapida, por arquivo). A validacao
    profunda — contagem de paginas, deteccao de PDF protegido por senha —
    acontece depois, no worker, porque exige abrir o documento.

    Nada disso depende de validacao no navegador: o `accept="application/pdf"`
    do input e conveniencia de interface, e qualquer cliente pode enviar um
    POST sem passar pela pagina.
    """

    arquivos = CampoMultiplosArquivos(
        label="Faturas em PDF",
        help_text="Selecione um ou mais arquivos PDF.",
    )

    def clean_arquivos(self):
        arquivos = self.cleaned_data["arquivos"]
        maximo = getattr(settings, "PDF_ARQUIVOS_POR_LOTE", 20)
        tamanho_max = getattr(settings, "PDF_TAMANHO_MAXIMO_BYTES", 25 * 1024 * 1024)

        if len(arquivos) > maximo:
            raise forms.ValidationError(
                f"Envie no maximo {maximo} arquivos por vez. Voce selecionou {len(arquivos)}."
            )

        for arquivo in arquivos:
            if arquivo.size == 0:
                raise forms.ValidationError(f"'{arquivo.name}' esta vazio.")

            if arquivo.size > tamanho_max:
                raise forms.ValidationError(
                    f"'{arquivo.name}' tem {arquivo.size / 1024 / 1024:.1f} MB e excede o "
                    f"limite de {tamanho_max / 1024 / 1024:.0f} MB."
                )

            # Magic bytes: a extensao e o content-type vem do cliente e podem
            # ser qualquer coisa. O que vale e o inicio real do conteudo.
            arquivo.seek(0)
            assinatura = arquivo.read(len(ASSINATURA_PDF))
            arquivo.seek(0)
            if assinatura != ASSINATURA_PDF:
                raise forms.ValidationError(
                    f"'{arquivo.name}' nao e um PDF de verdade — o conteudo nao "
                    f"comeca com a assinatura de PDF, seja qual for a extensao."
                )

        return arquivos


class EmpresaForm(forms.ModelForm):
    class Meta:
        model = Empresa
        fields = ["nome", "documento", "email_alerta", "whatsapp_alerta", "logo_url", "cor_primaria"]
        widgets = {
            "nome": forms.TextInput(attrs={"placeholder": "Nome da empresa"}),
            "documento": forms.TextInput(attrs={"placeholder": "CNPJ ou CPF"}),
            "email_alerta": forms.EmailInput(attrs={"placeholder": "alertas@empresa.com"}),
            "whatsapp_alerta": forms.TextInput(attrs={"placeholder": "+55 19 99999-9999"}),
            "logo_url": forms.URLInput(attrs={"placeholder": "https://.../logo.png"}),
            "cor_primaria": forms.TextInput(attrs={"placeholder": "#3b96f5"}),
        }

    def clean_cor_primaria(self):
        cor = (self.cleaned_data.get("cor_primaria") or "#3b96f5").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
            raise forms.ValidationError("Informe uma cor no formato #3b96f5.")
        return cor.lower()

    def clean_whatsapp_alerta(self):
        whatsapp = (self.cleaned_data.get("whatsapp_alerta") or "").strip()
        if whatsapp and not re.fullmatch(r"[+()\-\s\d]{8,30}", whatsapp):
            raise forms.ValidationError("Informe um WhatsApp valido, somente com numeros e sinais comuns.")
        return whatsapp


class UnidadeConsumidoraForm(forms.ModelForm):
    class Meta:
        model = UnidadeConsumidora
        fields = [
            "nome",
            "instalacao",
            "email_automacao",
            "tipo",
            "endereco",
            "limite_aumento_percentual",
            "email_alerta",
            "whatsapp_alerta",
            "ativa",
        ]
        widgets = {
            "nome": forms.TextInput(attrs={"placeholder": "Ex: Loja Centro"}),
            "instalacao": forms.TextInput(attrs={"placeholder": "Numero da instalacao CPFL"}),
            "email_automacao": forms.EmailInput(attrs={"placeholder": "cpfl-0000000@seudominio.com.br"}),
            "endereco": forms.TextInput(attrs={"placeholder": "Endereco da unidade"}),
            "limite_aumento_percentual": forms.NumberInput(attrs={"step": "0.01", "min": "0", "max": "300"}),
            "email_alerta": forms.EmailInput(attrs={"placeholder": "alertas@empresa.com"}),
            "whatsapp_alerta": forms.TextInput(attrs={"placeholder": "+55 19 99999-9999"}),
        }

    def clean_instalacao(self):
        instalacao = re.sub(r"\D", "", self.cleaned_data.get("instalacao") or "")
        if not 4 <= len(instalacao) <= 20:
            raise forms.ValidationError("Informe um numero de instalacao valido.")
        return instalacao

    def clean_email_automacao(self):
        return (self.cleaned_data.get("email_automacao") or "").strip().lower()

    def clean_limite_aumento_percentual(self):
        limite = self.cleaned_data.get("limite_aumento_percentual")
        if limite is None:
            return Decimal("20")
        if limite < 0 or limite > 300:
            raise forms.ValidationError("O limite deve ficar entre 0% e 300%.")
        return limite

    def clean_whatsapp_alerta(self):
        whatsapp = (self.cleaned_data.get("whatsapp_alerta") or "").strip()
        if whatsapp and not re.fullmatch(r"[+()\-\s\d]{8,30}", whatsapp):
            raise forms.ValidationError("Informe um WhatsApp valido, somente com numeros e sinais comuns.")
        return whatsapp