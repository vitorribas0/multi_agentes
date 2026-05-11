from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_view, name="index"),
    path("settings/", views.settings_view, name="settings"),
    path("api/chat/", views.chat_api, name="api"),
    path("api/upload/", views.upload_file, name="upload"),
    path("api/download/", views.download_file, name="download"),
    path("api/settings/", views.settings_api, name="settings_api"),
    path("api/tool-detail/", views.tool_detail_api, name="tool_detail_api"),
    path("api/skills/", views.skill_list_api, name="skill_list_api"),
    path("api/skills/save/", views.skill_save_api, name="skill_save_api"),
    path("api/skills/delete/", views.skill_delete_api, name="skill_delete_api"),
    path("api/sessions/", views.session_list_api, name="session_list_api"),
    path("api/sessions/detail/", views.session_detail_api, name="session_detail_api"),
    path("api/sessions/delete/", views.session_delete_api, name="session_delete_api"),
]
