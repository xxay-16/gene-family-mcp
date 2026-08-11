from django.shortcuts import render


def predictor_page(request):
    return render(request, 'cis_elements/predictor.html')
