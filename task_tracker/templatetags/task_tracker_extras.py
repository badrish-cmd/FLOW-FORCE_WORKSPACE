from django import template

register = template.Library()


@register.filter
def get_item(value, key):
    if value is None:
        return ""
    return value.get(key, "")


@register.filter
def replace_underscore(value, replacement=" "):
    if value is None:
        return ""
    return str(value).replace("_", replacement)
