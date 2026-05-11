import uuid

from django.db import models


class Skill(models.Model):
    """Skill personalizada cadastrada pelo usuário."""

    id           = models.CharField(max_length=32, primary_key=True)
    name         = models.CharField(max_length=200)
    slug         = models.CharField(max_length=200, unique=True)
    when_to_use  = models.TextField()
    instructions = models.TextField()
    code         = models.TextField(blank=True, default="")
    active       = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "when_to_use": self.when_to_use,
            "instructions": self.instructions,
            "code": self.code,
            "active": self.active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class ChatSession(models.Model):
    """Sessão de chat — agrupa as mensagens de uma conversa."""

    id         = models.CharField(max_length=32, primary_key=True)
    title      = models.CharField(max_length=300)
    model      = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    def to_dict(self, include_messages: bool = False) -> dict:
        d: dict = {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_messages:
            d["history"] = [m.to_dict() for m in self.messages.order_by("created_at")]
        return d


class Message(models.Model):
    """Mensagem individual dentro de uma ChatSession."""

    ROLES = [("user", "user"), ("assistant", "assistant"), ("tool", "tool")]

    session      = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role         = models.CharField(max_length=20, choices=ROLES)
    content      = models.TextField(blank=True, default="")
    tools_called = models.JSONField(default=list, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.role}] {self.content[:60]}"

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "toolsCalled": self.tools_called,
        }
