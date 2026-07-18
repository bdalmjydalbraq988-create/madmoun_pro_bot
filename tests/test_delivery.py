from app.keyboards import delivery_keyboard
from app.services.delivery import delivery_html, first_http_url, is_placeholder_delivery


def test_delivery_link_has_open_and_copy_buttons() -> None:
    delivery = "https://serviceactivation.google/subscription/example"
    keyboard = delivery_keyboard(delivery)

    assert first_http_url(delivery) == delivery
    assert keyboard.inline_keyboard[0][0].url == delivery
    assert keyboard.inline_keyboard[1][0].copy_text.text == delivery


def test_long_delivery_link_avoids_invalid_inline_button_payloads() -> None:
    delivery = "https://example.com/" + ("a" * 300)
    keyboard = delivery_keyboard(delivery)

    assert keyboard is None
    assert f'href="{delivery}"' in delivery_html(delivery)


def test_old_generic_success_text_is_detected_for_safe_recovery() -> None:
    assert is_placeholder_delivery("تم تنفيذ وتفعيل الخدمة بنجاح لدى المورد.")
    assert not is_placeholder_delivery("login: password")
