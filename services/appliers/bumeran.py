from services.applier import register
from services.appliers._navent_base import NaventApplier


@register
class BumeranApplier(NaventApplier):
    platform_name = "bumeran"
    cv_profile_name = "local"
